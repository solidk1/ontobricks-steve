# OntoBricks Deployment Guide

## Overview

OntoBricks is a **container-ready ASGI process**. It runs anywhere a container
runs — Azure Container Apps, ECS, Kubernetes, a plain VM — against **any
PostgreSQL 14+ server**.

Databricks is a **connector, not a platform dependency**. When configured, it
provides Unity Catalog source metadata, SQL Warehouse query execution, the Delta
graph engine, UC Volume document attachments, Lakeview dashboards, and optionally
Foundation Model APIs for the LLM. None of that is required to start the app, and
the LLM can equally be OpenAI, Azure OpenAI, vLLM, Ollama or a proxy.

> **The Databricks Apps deploy is gone.** The asset bundle (`databricks.yml`,
> `app.yaml.template`, `.databricksignore`), `scripts/deploy*` and the
> `bootstrap/` scripts were removed in **v0.7.1**. There is no `make deploy`, no
> `databricks bundle deploy`, and no `app.yaml`. If you are following an older
> copy of this guide, stop — the commands in it no longer exist.

### What this guide covers

| Section | Topic |
|---|---|
| [1](#1-local-development) | Local development |
| [2](#2-database-setup) | PostgreSQL setup and the co-tenancy contract |
| [3](#3-running-in-a-container) | Building and running the container |
| [4](#4-authentication) | OIDC login and app roles |
| [5](#5-llm-provider) | LLM provider configuration |
| [6](#6-unity-catalog-permissions) | Unity Catalog grants (only if using the Databricks connector) |
| [7](#7-graph-db-backends) | Graph DB backends |
| [8](#8-mcp-server) | MCP server |
| [9](#9-observability) | Logging, health, MLflow traces |
| [10](#10-deployment-checklist) | Deployment checklist |
| [11](#11-troubleshooting) | Troubleshooting |
| [12](#12-production-considerations) | Production considerations |

`.env.example` is the authoritative reference for every variable. `README.md`
§"Running in a container" is the short version of sections 2–5.

---

## 1. Local development

```bash
git clone <repo> && cd ontobricks

# uv, if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# `--extra lakebase` is required for every Postgres target. The extra is named
# for historical reasons; it is plain psycopg + psycopg-pool.
make install          # uv venv && uv sync --frozen --extra lakebase --extra pitfalls
```

Create `.env` from `.env.example`. The minimum for local work:

```bash
PGHOST=localhost
PGDATABASE=ontobricks
PGUSER=postgres
PGPASSWORD=...
ONTOBRICKS_PG_AUTH=password
ONTOBRICKS_AUTH_ENABLED=false        # local only — see §4
SECRET_KEY=<random>
```

Then:

```bash
make run     # or: make dev  — binds 127.0.0.1:8000 with auto-reload
```

`run.py` binds loopback and enables reload unless `ONTOBRICKS_CONTAINERIZED=true`
(see §3). Set `ONTOBRICKS_NO_RELOAD=1` to disable reload while staying on
loopback.

---

## 2. Database setup

OntoBricks stores all structured registry data — domains, versions, permissions,
schedules, app roles, global config — in PostgreSQL via `PostgresRegistryStore`.

### The co-tenancy contract

OntoBricks installs into an **existing database as one new schema and touches
nothing outside it**. The testable invariant:

> `DROP SCHEMA ontobricks CASCADE;` removes OntoBricks completely, with zero
> residue anywhere else in the instance.

Consequences you should know about:

- **No extensions are required.** `CREATE EXTENSION` is database- or
  server-scoped, so it would outlive the schema drop and affect co-tenants. On
  Azure it is additionally gated behind the server-level `azure.extensions`
  allowlist. Both functions OntoBricks needs are in core PostgreSQL: `sha256()`
  (PG 11+) and `gen_random_uuid()` (PG 13+).
- **`sha256(convert_to(...))` cannot be used directly in a generated column**,
  because `convert_to` is `STABLE` and generated columns require `IMMUTABLE`. The
  DDL therefore creates a small in-schema `sha256_utf8()` wrapper. This was found
  the hard way against a real PG 16.15 server; documentation alone did not
  predict it.
- **`search_path` never includes `public`.**
- PostgreSQL **14+** is the floor.

### Creating the schema and role (once, by a DBA)

```sql
CREATE SCHEMA ontobricks;
CREATE ROLE ontobricks_app LOGIN;
GRANT USAGE, CREATE ON SCHEMA ontobricks TO ontobricks_app;
ALTER ROLE ontobricks_app SET search_path = ontobricks;
```

The app creates its own tables inside that schema on **Settings → Registry →
Initialize** (or on first use). `azure_pg_admin` is sufficient; superuser is not
required, and OntoBricks never asks for it.

### How the password is produced — `ONTOBRICKS_PG_AUTH`

There is **one** set of connection variables for every server. Only the
credential differs.

| Mode | Credential | When |
|---|---|---|
| `entra` | Microsoft Entra ID access token used as the password, scope `https://ossrdbms-aad.database.windows.net/.default`, minted per physical connection via `DefaultAzureCredential` and refreshed with a 300s margin | Azure Database for PostgreSQL with Entra authentication. `PGUSER` is the Entra principal name; map it once with `pgaadauth_create_principal`. |
| `lakebase` | Short-lived JWT minted from the Databricks Postgres API | Databricks Lakebase. It is a PostgreSQL endpoint like any other; only the credential differs. |
| `password` | Plain `PGPASSWORD` | Self-hosted, RDS, local, anything else. |

Left unset, the mode is inferred: an Azure Database for PostgreSQL host in any
Azure cloud (`.azure.com`, `.chinacloudapi.cn`, `.usgovcloudapi.net`) implies
`entra`, otherwise `lakebase`. Set it explicitly in production.

The retired `LAKEBASE_SCHEMA` / `LAKEBASE_DATABASE` / `LAKEBASE_PROJECT` /
`LAKEBASE_BRANCH` variables are gone. Use `PGHOST` / `PGDATABASE` / `PGUSER` /
`PGPORT` / `PGSSLMODE` plus `ONTOBRICKS_PG_SCHEMA`.

### Connection notes

- **Connect on 5432 (direct), not 6432 (PgBouncer).** Azure's PgBouncer runs
  `pool_mode=transaction` with `max_prepared_statements=0`, which conflicts with
  psycopg3's `prepare_threshold=5`. OntoBricks keeps its own in-process pool
  (`ONTOBRICKS_PG_POOL_MIN` / `_MAX`, default `1`/`8`), so an external pooler adds
  nothing.
- For `PGSSLMODE=verify-full`, the image must contain the server's root CA.
- The **graph** schema is per-domain configuration, not an environment
  variable: it comes from `graph_engine_config.schema` (Domain → Information →
  Knowledge Graph), falling back to the registry Volume's schema segment and
  then to `DEFAULT_GRAPH_SCHEMA`.

---

## 3. Running in a container

```bash
ONTOBRICKS_CONTAINERIZED=true python run.py      # == make prod
```

`ONTOBRICKS_CONTAINERIZED=true` is what makes uvicorn bind `0.0.0.0` on `$PORT`
and put sessions and logs under `/tmp`. **Without it the process binds
`127.0.0.1` and nothing outside the container can reach it.**

This repo deliberately ships **no Dockerfile** — the image is yours to build
around that entrypoint. Whatever you build must contain:

- the source and `uv.lock` (install with `uv sync --frozen --extra lakebase`),
- **`documentation/`** — the Help Center serves markdown from it at runtime; an
  image without it renders an empty Help Center,
- the server's root CA, if `PGSSLMODE=verify-full`.

`uv.lock` must be free of internal-proxy URLs before you build; see
`.claude/skills/deploy/SKILL.md` for the checks and
`.cursor/09-package-management.mdc` for the policy.

### Single replica — not optional

APScheduler runs **in-process** and sessions are on local disk. More than one
instance duplicates every scheduled build; scaling to zero stops the scheduler
entirely. On Azure Container Apps that means `minReplicas: 1, maxReplicas: 1` —
its defaults violate both. On Kubernetes it means `replicas: 1` **and**
`strategy: Recreate`, because a `RollingUpdate` briefly runs two pods; ready-made
manifests are in `deploy/azure/k8s/base/` (plus `overlays/` per cloud), and `tests/units/deploy/test_aks_manifests.py`
fails if either is changed.

---

## 4. Authentication

Off the Databricks Apps platform there are no proxy identity headers, so
OntoBricks runs the OAuth 2.0 **authorization-code + PKCE** flow itself. With
Databricks as the IdP that means a **custom OAuth app integration** (account
console → App connections), registered with `https://<host>/auth/callback` as its
redirect URI. One login yields the user's email, their groups, and a Databricks
user token — the last enabling per-user Unity Catalog enforcement on interactive
queries.

```bash
ONTOBRICKS_AUTH_ENABLED=true                 # the default
ONTOBRICKS_OIDC_CLIENT_ID=...
ONTOBRICKS_OIDC_CLIENT_SECRET=...            # omit for a public (PKCE-only) client
ONTOBRICKS_OIDC_REDIRECT_URI=https://<host>/auth/callback
ONTOBRICKS_OIDC_SCOPES="all-apis offline_access"
ONTOBRICKS_BOOTSTRAP_ADMIN=you@example.com
```

`ONTOBRICKS_AUTH_ENABLED` defaults to **true** — it fails closed. Set it `false`
only for local development.

App-level access lives in the registry's `app_roles` table, not in a Databricks
App ACL. `ONTOBRICKS_BOOTSTRAP_ADMIN` seeds the first admin, without which a
fresh deployment has nobody able to grant access; it applies only while no admin
exists, so a deliberate revoke is not undone. Manage grants from **Settings → App
access**, or via `GET`/`POST /settings/app-roles{,/grant,/revoke}`. The last admin
cannot be revoked.

Set `ONTOBRICKS_SECURE_COOKIES=true` behind TLS. Cookie security and access
control are independent flags on purpose — conflating them is how a deployment
ends up with neither.

---

## 5. LLM provider

Agents and SQL Wizard call exactly one kind of endpoint:

```
POST {ONTOBRICKS_LLM_BASE_URL}/chat/completions
Authorization: Bearer {ONTOBRICKS_LLM_API_KEY}
{"model": "{ONTOBRICKS_LLM_MODEL}", "messages": [...]}
```

Any OpenAI-compatible provider works, and **Databricks is one of them rather than
a special case** — Foundation Model APIs serve that shape at
`https://<workspace>/serving-endpoints`, with the serving-endpoint name as the
model.

```bash
# OpenAI
ONTOBRICKS_LLM_BASE_URL=https://api.openai.com/v1
ONTOBRICKS_LLM_API_KEY=sk-...
ONTOBRICKS_LLM_MODEL=gpt-4o-mini

# Databricks Foundation Model API
ONTOBRICKS_LLM_BASE_URL=https://<workspace>/serving-endpoints
ONTOBRICKS_LLM_API_KEY=<pat>
ONTOBRICKS_LLM_MODEL=databricks-claude-sonnet-4

# Ollama / bare vLLM — omit the key entirely; a Bearer header carrying nothing
# is rejected by those servers
ONTOBRICKS_LLM_BASE_URL=http://localhost:11434/v1
ONTOBRICKS_LLM_MODEL=llama3.2
```

**Nothing is inferred and nothing falls back.** A configured Databricks workspace
is not an LLM provider. Leave `ONTOBRICKS_LLM_BASE_URL` unset and AI features fail
with an error naming the variable — they do not quietly reach for the workspace,
and there is no endpoint auto-discovery picking whichever model happens to be
ready.

`ONTOBRICKS_LLM_MODEL` is the declared default. `ONTOBRICKS_LLM_MODELS`
(comma-separated) populates the per-domain picker in **Settings → LLM**, since
most providers have no listing endpoint worth querying. The provider and
credential are deliberately **not** editable in the UI: an API key does not belong
in a settings table the UI reads and writes.

Every provider above and the URL each resolves to is pinned in
`tests/eval/datasets/engine_base/baseline.jsonl`, asserted on every test run.

---

## 6. Unity Catalog permissions

**Skip this section entirely if you are not using the Databricks connector.**

OntoBricks authenticates to Databricks as a service principal
(`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`) or, for interactive
queries, as the logged-in user via their OIDC token. Either way, the identity
needs Unity Catalog grants — reach-through to a warehouse is not data access.

Throughout this section that identity is written `<principal>` (a UUID/client-id
for an SP, an email for a user).

> **Privilege mental model.** To reference any UC object you need `USE CATALOG`
> on its catalog and `USE SCHEMA` on its schema. To read table data you need
> `SELECT`. To create tables or views in a schema you need schema-level
> `CREATE TABLE` / `CREATE VIEW`. To replace an object that **already exists**
> (including `CREATE OR REPLACE` over one) you must own it or hold `MANAGE`.
> Volumes are separate: files under `/Volumes/...` need `READ VOLUME` and
> `WRITE VOLUME`. `ALL PRIVILEGES` is convenient for onboarding but implies
> `MANAGE`, which is broader than the app needs day to day.

### 6.1 What OntoBricks does in UC

| # | Operation | Target | Privilege |
|---|---|---|---|
| 1 | `SHOW CATALOGS/SCHEMAS/TABLES/VOLUMES`, `DESCRIBE`, `information_schema` lookups (Data Source picker) | Source + registry catalogs | `USE CATALOG` + `USE SCHEMA` + `SELECT` on browsed tables |
| 2 | `SELECT` on source tables referenced by R2RML `sql_query` entries | Each source table/view | `SELECT` |
| 3 | `CREATE OR REPLACE VIEW <catalog>.<schema>.triplestore_<domain>_V<n>` (Delta engine) | Registry schema | `CREATE VIEW`; plus `MANAGE`/ownership if a prior build left the object behind |
| 4 | `SELECT subject, predicate, object` from the triplestore view | That view | `SELECT` (inherited as owner from step 3) |
| 5 | File I/O under `/Volumes/<catalog>/<schema>/<volume>/` (document uploads, registry artefacts) | Registry volume | `READ VOLUME` + `WRITE VOLUME` |
| 6 | `POST /api/2.1/unity-catalog/volumes` — only from **Settings → Registry → Initialize** when the volume does not exist | Registry schema | `CREATE VOLUME` (skip if you create it manually) |

All of the above run through the SQL Warehouse named by
`DATABRICKS_SQL_WAREHOUSE_ID`. `CAN_USE` on the warehouse covers compute; UC
controls data.

### 6.2 Registry grants (minimum viable set)

Run once as a workspace admin, or anyone with `MANAGE` on the catalog/schema:

```sql
GRANT USE CATALOG   ON CATALOG `<registry_catalog>`                        TO `<principal>`;

GRANT USE SCHEMA    ON SCHEMA  `<registry_catalog>`.`<registry_schema>`    TO `<principal>`;
GRANT CREATE TABLE  ON SCHEMA  `<registry_catalog>`.`<registry_schema>`    TO `<principal>`;
GRANT CREATE VIEW   ON SCHEMA  `<registry_catalog>`.`<registry_schema>`    TO `<principal>`;

-- Registry Volume (document uploads, registry artefacts)
GRANT READ VOLUME   ON VOLUME  `<registry_catalog>`.`<registry_schema>`.`<registry_volume>` TO `<principal>`;
GRANT WRITE VOLUME  ON VOLUME  `<registry_catalog>`.`<registry_schema>`.`<registry_volume>` TO `<principal>`;

-- Optional — only if the Settings UI should be able to create the volume itself
GRANT CREATE VOLUME ON SCHEMA  `<registry_catalog>`.`<registry_schema>`    TO `<principal>`;
```

Shorter, broader, fine for a dev workspace:

```sql
GRANT USE CATALOG    ON CATALOG `<registry_catalog>`                     TO `<principal>`;
GRANT ALL PRIVILEGES ON SCHEMA  `<registry_catalog>`.`<registry_schema>` TO `<principal>`;
```

Prefer the explicit list in production: `ALL PRIVILEGES` implies `MANAGE`.

### 6.3 Source data grants

For every table or view referenced in an R2RML mapping (anything in a domain's
**Data Sources** tab):

```sql
GRANT USE CATALOG ON CATALOG `<source_catalog>`                   TO `<principal>`;
GRANT USE SCHEMA  ON SCHEMA  `<source_catalog>`.`<source_schema>` TO `<principal>`;
GRANT SELECT      ON TABLE   `<source_catalog>`.`<source_schema>`.`<table>` TO `<principal>`;
```

Grant `SELECT` on the schema instead of per-table only if you are comfortable with
the breadth.

---

## 7. Graph DB backends

Each domain picks its graph engine in **Domain → Information → Knowledge Graph**.
Three are supported, all pluggable through `GraphDBFactory` — see
`documentation/graphdb-integration.md` to add one.

| Engine | Requires | Notes |
|---|---|---|
| `postgres` | The registry Postgres | Default. The app streams R2RML rows in `fetchmany` batches and ingests via `COPY FROM STDIN` + `INSERT … ON CONFLICT DO NOTHING`. |
| `databricks` | SQL Warehouse + UC grants (§6) | Materialises a Delta view for governance and lineage. |
| `neo4j` | A reachable Bolt endpoint | Password resolved live from a Databricks secret scope/key chosen in **Settings → Backend → Neo4j**, or `NEO4J_PASSWORD` for `auth_method: "basic"`. See `documentation/neo4j-requirements.md`. |

The persisted config key for the Postgres bucket is `postgres`. Configs written by
0.7 and earlier used `lakebase`; those are read transparently and rewritten to the
canonical key on the next save.

> The Lakebase `managed_synced` write mode and `SyncedTableManager` were removed
> in v0.7.1 along with the Apps deploy. `app_managed` streaming is the only write
> path, and it works against any Postgres.

---

## 8. MCP server

The MCP server lives in `src/mcp-server/` and exposes knowledge-graph tools to
MCP clients. It is a separate ASGI process pointed at the main app:

```bash
ONTOBRICKS_URL=https://<main-app-host> python src/mcp-server/mcp_server.py
```

Run it as a second container alongside the main app. Its former `app.yaml` and
`deploy-mcp-server.sh` were removed with the Apps path. See
`documentation/mcp.md` for the tool surface.

---

## 9. Observability

### Logging

| Variable | Default | Effect |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | text | `json` emits one JSON object per line, for log aggregation |
| `ONTOBRICKS_THREAD_POOL_SIZE` | `20` | Concurrent blocking work |

Every non-static request is logged with method, path, status and duration.

### Health

`GET /health` is the single readiness probe. It is anonymous — in the bypass list
of `PermissionMiddleware`, `CSRFMiddleware` and `RequestTimingMiddleware` — so a
load balancer or k8s probe reaches it without a session cookie. There is no
`/healthz`; `/health/detailed` was folded into `/health`.

It returns `{status, summary:{ok,warnings,errors}, checks:[…]}` where each check
is `{name, label, status, detail, duration_ms}` and the top-level `status` is the
worst severity across all of them. Checks include `runtime`, `filesystem.*`,
`postgres`, `postgres.permissions`, `graphdb.postgres`, `registry.cfg`,
`registry.volume_read`, `registry.volume_write`, `registry.uc_schema_ddl`, plus
`databricks.auth` / `databricks.warehouse` / `databricks.cloudfetch` when the
connector is configured.

> **It always returns HTTP 200.** External probes must read the top-level
> `status` and `summary.errors` rather than the HTTP code, so one flickering
> dependency does not pull the app out of rotation.

### Agent traces

Agents are instrumented with MLflow tracing, which degrades to a no-op when
MLflow is not configured.

| Variable | Default | Effect |
|---|---|---|
| `MLFLOW_TRACKING_URI` | local file store | `databricks` persists traces to a workspace |
| `ONTOBRICKS_MLFLOW_EXPERIMENT` | `ontobricks-agents` | Experiment name, auto-prefixed with `/Shared/` on Databricks |

Every agent call produces a span tree:

```
AGENT (run_agent)
├── LLM (agent:llm)          — endpoint, model, tokens, latency
├── TOOL (tool:get_metadata) — arguments, result
├── LLM (agent:llm)          — next iteration
└── ...
```

On Databricks, view them under **Machine Learning → Experiments →
`/Shared/ontobricks-agents` → Traces**.

---

## 10. Deployment checklist

**Before building**

- [ ] `uv run --frozen pytest -q -m "not scenario"` green
- [ ] `uv.lock` free of internal-proxy URLs, and in sync with `pyproject.toml`
- [ ] Image contains `documentation/`, and the root CA if `PGSSLMODE=verify-full`

**Database**

- [ ] PostgreSQL 14+ reachable on **5432**, not a PgBouncer port
- [ ] Schema + role created; `ONTOBRICKS_PG_AUTH` set explicitly
- [ ] For `entra`: `PGUSER` mapped via `pgaadauth_create_principal`, and the
      container's managed identity can mint tokens

**App**

- [ ] `ONTOBRICKS_CONTAINERIZED=true` and `PORT` set
- [ ] `SECRET_KEY` set
- [ ] `minReplicas == maxReplicas == 1`
- [ ] `ONTOBRICKS_OIDC_CLIENT_ID` / `_REDIRECT_URI` set (auth is on by default)
- [ ] `ONTOBRICKS_BOOTSTRAP_ADMIN` set
- [ ] `ONTOBRICKS_SECURE_COOKIES=true` behind TLS

**Optional connectors**

- [ ] `ONTOBRICKS_LLM_BASE_URL` / `_API_KEY` / `_MODEL` for AI features
- [ ] `DATABRICKS_HOST` + `DATABRICKS_SQL_WAREHOUSE_ID` + UC grants (§6)

**After deploying**

- [ ] `GET /health` — read `status`, not the HTTP code
- [ ] **Re-check a minute later** — a dependency failure can surface well after a
      healthy-looking start
- [ ] **Settings → Registry → Initialize** if the registry is empty
- [ ] Log in, and confirm the bootstrap admin was granted (**Settings → App
      access**)

---

## 11. Troubleshooting

**App starts but is unreachable.** `ONTOBRICKS_CONTAINERIZED` is not `true`, so
uvicorn bound `127.0.0.1`. This is the single most common container mistake.

**Crash ~45s after a successful start.** Almost always `uv.lock` pointing at an
internal proxy: wheels the proxy has not cached fail to download. Re-lock against
the public index.

**`prepared statement "..." already exists` or `DuplicatePreparedStatement`.** You
are connected through PgBouncer in transaction mode. Use port 5432.

**Postgres auth failures every ~30–60 min on Azure.** An Entra token expired and
was not refreshed. Tokens are minted per physical connection with a 300s margin;
if you pinned an external pooler in front, that assumption breaks.

**"generation expression is not immutable" during Initialize.** The in-schema
`sha256_utf8()` wrapper is missing — the schema was created by an older version.
Re-run Initialize.

**Nobody can log in on a fresh deployment.** `ONTOBRICKS_AUTH_ENABLED` defaults to
true and no admin exists. Set `ONTOBRICKS_BOOTSTRAP_ADMIN` and restart; it applies
while no admin exists.

**AI features fail with "No LLM provider configured".** Working as designed —
`ONTOBRICKS_LLM_BASE_URL` is unset and nothing is inferred from a Databricks
workspace. See §5.

**Empty model picker in Domain Settings.** `ONTOBRICKS_LLM_MODELS` is unset, so
only `ONTOBRICKS_LLM_MODEL` is offered.

**Empty Help Center.** `documentation/` was not copied into the image.

**Scheduled builds run twice.** More than one replica. See §3.

**Knowledge Graph Sync fails on `CREATE OR REPLACE VIEW`.** A previous build left
an object the principal does not own. Grant `MANAGE` on it or drop it. See §6.

---

## 12. Production considerations

### Security

- Never commit `.env` or secrets.
- Provider credentials (`ONTOBRICKS_LLM_API_KEY`, `DATABRICKS_CLIENT_SECRET`,
  `PGPASSWORD`) belong in your platform's secret store, injected as environment
  variables. They are deliberately not settable through the UI, so they never
  reach the registry's settings table, domain exports, or a log line.
- Prefer workload identity over static secrets where the platform offers it —
  `ONTOBRICKS_PG_AUTH=entra` with a managed identity needs no stored password.
- `ONTOBRICKS_AUTH_ENABLED=true` (the default) and
  `ONTOBRICKS_SECURE_COOKIES=true` behind TLS.

### Performance

- Size the SQL Warehouse appropriately and enable auto-stop.
- Tune `ONTOBRICKS_PG_POOL_MIN` / `_MAX` to the server's connection budget,
  remembering the single-replica constraint means one pool.
- Below the in-memory triple cap, graph metrics are computed in-process; above
  it, set `ONTOBRICKS_ANALYTICS_JOB_ENABLED=true` and provide the serverless job
  (`resources/graph_analytics.job.yml` defines it, but this repo no longer deploys
  it — create it with your own bundle or the Jobs UI).

### Updating

```bash
git pull
uv run --frozen pytest -q -m "not scenario"
# rebuild and roll out the image on your platform
```

Registry schema changes are applied idempotently on startup and via **Settings →
Registry → Initialize**.
