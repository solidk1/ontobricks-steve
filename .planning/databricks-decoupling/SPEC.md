# SPEC — Decouple OntoBricks from Databricks Apps and Lakebase

- **Status:** approved design, not yet implemented
- **Date:** 2026-09-01
- **Slug:** `databricks-decoupling`
- **Target version:** next minor (bump `pyproject.toml` in P7)

## 1. Goal

Make OntoBricks deployable as an ordinary container image against an ordinary
PostgreSQL instance, by removing two dependencies:

- **Databricks Apps** as the hosting platform (runtime env injection, the
  `x-forwarded-*` identity headers, the App ACL, `app.yaml`, the asset bundle).
- **Lakebase** as the database (endpoint discovery through the Databricks API,
  JWT-as-password minting, project/branch provisioning).

Databricks remains a **connector**: OIDC login, SQL Warehouse source reads, the
Delta triple-store engine, UC Volume attachments, Lakeview dashboards,
`ai_parse_document`, and Foundation Model API.

### Non-goals

- Removing Databricks as a data source. Reading UC/Delta tables is the product's
  purpose and stays.
- Supporting a non-Databricks identity provider. Login is Databricks OIDC.
- Moving binary attachments off UC Volume.
- Multi-replica horizontal scaling (see §12.3).

## 2. Decisions

| Area | Decision |
|---|---|
| Scope | Remove Apps-as-host and Lakebase-as-database; Databricks stays a connector |
| Identity | Built-in OIDC (authorization-code + PKCE) against a Databricks custom OAuth app integration |
| Background auth | Service-principal client credentials (`DATABRICKS_CLIENT_ID` / `_SECRET`); no workload identity federation |
| Interactive queries | Run as the logged-in user's token → per-user UC enforcement |
| Engines | Postgres + Delta. Neo4j deleted |
| Registry | Any PostgreSQL ≥ 14 |
| App-level roles | New `app_roles` table + `ONTOBRICKS_BOOTSTRAP_ADMIN` |
| Auth default | `ONTOBRICKS_AUTH_ENABLED` defaults **on** (fail closed) |
| Attachments | UC Volume, unchanged |
| LLM | OpenAI-compatible base URL; Databricks FMAPI as a preset |
| Artifact | One container image, MCP mounted at `/mcp`, compose for local dev |
| Postgres install | One new schema in an existing database, no extensions, residue-free uninstall |

### 2.1 Connector-gated capabilities kept as-is

These stay exactly as they are and become "available when the Databricks
connector is configured", with no portable fallback:

| Capability | Code | Note |
|---|---|---|
| Graph analytics offload | `resources/graph_analytics.job.yml` | Already flag-gated; in-process `networkx` remains the path below the triple cap |
| Document text extraction | `DocumentExtractor` (`ai_parse_document`) | **Document import does not work without the connector** — see §13 |
| Dashboard listing | `DashboardService` (Lakeview + legacy) | Unchanged |
| Binary attachments | `uc/VolumeFileService.py` | Unchanged, per decision |
| MLflow tracing | `agents/tracing.py` | Already switches on `MLFLOW_TRACKING_URI`; default becomes a local file store, `databricks` still supported |

## 3. Current-state assessment

Measured on `master` at `7b272092`:

| Concern | Location | Seam quality |
|---|---|---|
| Registry store | `back/objects/registry/store/` — ABC + factory, one impl (`lakebase/store.py`, 4037 LOC of plain Postgres SQL) | good; only auth is Lakebase-specific |
| Triple store | `back/core/graphdb/` — `GraphDBBackend` ABC, engines `lakebase` / `delta` / `neo4j` | good |
| Postgres connection | `back/core/databricks/lakebase/LakebaseAuth.py` (705 LOC), `LakebaseConnectionPool.py` (345 LOC) | **the whole Lakebase lock-in** |
| Identity / RBAC | 25 `x-forwarded-*` reads; SCIM group lookup; App ACL via `list_app_principals` | needs replacement |
| Runtime predicate | `is_databricks_app()` — 37 callers; `DATABRICKS_APP_PORT` — 17 more | conflated, see §5.4 |
| Source data | `SQLWarehouse.py`, reached only via `DatabricksClient` | narrow, and stays |
| LLM | `agents/engine_base.py:84` → `{host}/serving-endpoints/{name}/invocations` | one function |
| Analytics | serverless Databricks job, already flag-gated | stays, connector-gated |
| Deploy | `app.yaml.template`, `databricks.yml`, `scripts/deploy.sh` | replaced |

79 of 309 Python files mention Databricks; `lakebase` appears 1134 times across
54 files. Tests: 347 files, 73,672 LOC.

Two findings that shaped the design:

1. **Per-user UC permissions are not enforced today.** All data queries run as
   the app's service principal; the user token is used only for App ACL and SCIM
   group reads. UC therefore enforces the SP's grants, and every OntoBricks user
   sees whatever that one SP can see.
2. **Domain-level roles are already in Postgres** (`domain_permissions` table).
   Only *app-level* access depends on the Databricks App ACL.

## 4. Target architecture

```
┌── one container image ─────────────────────────────┐
│ uvicorn → FastAPI                                  │
│   ├─ front (Jinja2) + api (REST + GraphQL)         │
│   ├─ /mcp          FastMCP, mounted mode           │
│   └─ APScheduler   builds, reasoning, analytics    │
└────────────────────────────────────────────────────┘
      │ psycopg pool              │ HTTPS
      ▼                           ▼
  PostgreSQL ≥ 14           Databricks workspace (connector)
  └─ one schema             ├─ OIDC custom app → login + user token
     ├─ registry tables     ├─ SQL Warehouse → source reads, Delta engine
     └─ graph tables        ├─ UC Volume     → attachments
                            └─ FMAPI or any OpenAI-compatible endpoint
```

## 5. Seams

### 5.1 `PostgresConnectionPool`

Replaces `LakebaseAuth` (705 LOC) + `LakebaseConnectionPool` (345 LOC) with
roughly 120 LOC over `psycopg_pool`. Deleted: Databricks-API endpoint discovery,
JWT minting, pre-expiry refresh.

- Config: `DATABASE_URL`, or discrete `PGHOST` / `PGPORT` / `PGDATABASE` /
  `PGUSER` / `PGPASSWORD` / `PGSSLMODE`.
- `search_path` is set through the conninfo (`options=-csearch_path=<schema>`),
  not a per-checkout `SET` — one fewer round trip and PgBouncer-safe.
- Retains one idea from `LakebaseAuth`: an optional `password_provider`
  callable, so IAM-auth Postgres (RDS IAM, Cloud SQL) — or Lakebase itself —
  can return later as a plug-in.
- Pool bounds default to min 1 / max 8, both configurable (§6).

### 5.2 `IdentitySession`

Replaces 25 `x-forwarded-*` reads.

- `GET /auth/login` → Databricks authorize URL (authorization code + PKCE,
  scopes `all-apis offline_access`).
- `GET /auth/callback` → validates `state`, exchanges the code, calls SCIM
  `/Me` for email and groups, stores tokens in the existing server-side session
  (`session_dir`, itsdangerous).
- Middleware populates `request.state.user_email`, `.user_groups`,
  `.databricks_user_token`. **Call sites read `request.state` only, never
  headers.** Several already read `request.state.user_email` with a header
  fallback, so this narrows existing code rather than rewriting it.
- Refresh token used to retry once on a 401 from a Databricks call.
- **User token** (per-user UC enforcement): UC metadata browse, table preview /
  sample, mapping validation probes, SQL wizard.
- **Service principal**: domain build and materialization, reasoning, graph
  analytics, every `TaskManager` / APScheduler job, and any request with no
  session. Long builds cannot use a ~1 h user token.
- `SQLWarehouse` pools connections by `(host, warehouse_id)`; the user-token
  path keys pools per user, so the pool cap is per-identity.

### 5.3 `AppRoleService` + `app_roles`

Replaces `list_app_principals`. One table added to `schema.sql`, seeded from
`ONTOBRICKS_BOOTSTRAP_ADMIN` on `initialize()`, plus an admin screen to grant
and revoke. `PermissionService.get_app_role()` reads the table.
`domain_permissions` is untouched.

### 5.4 `RuntimeEnv` — the highest-risk change

`is_databricks_app()` (37 callers) and `DATABRICKS_APP_PORT` (17 more) currently
answer three unrelated questions with one boolean:

1. *Am I containerized?* — port, session dir, log format.
2. *Is auth enforced?* — `PermissionService` is inert when false ("in local mode
   every user has unrestricted access").
3. *Is Databricks reachable?* — connector code paths.

Split into `PORT`, `ONTOBRICKS_AUTH_ENABLED` (default **on**), and
`DatabricksConnector.is_configured()`. This is where bugs will hide: a site that
means one thing today can silently get a different answer, and the auth one
fails **open** if mis-split.

### 5.5 `LLMClient`

`agents/engine_base.py` posts to `{base_url}/chat/completions`, with
`{host}/serving-endpoints/{name}/invocations` as the Databricks preset (those
endpoints already speak the OpenAI chat shape). Config:
`ONTOBRICKS_LLM_BASE_URL`, `ONTOBRICKS_LLM_API_KEY`, `ONTOBRICKS_LLM_MODEL`.

## 6. Co-tenancy Postgres contract

OntoBricks installs into an **existing database as one new schema, touching
nothing outside it**. The testable invariant:

> `DROP SCHEMA ontobricks CASCADE;` removes OntoBricks completely, with zero
> residue anywhere else in the instance.

Consequences:

- **No database-scoped objects, so no `pgcrypto`.** Not a privilege limit — an
  extension is database-scoped, outlives the schema drop, and is shared with
  co-tenants. Both functions it provided are in core Postgres:
  - `digest(object,'sha256')` in the `object_hash` generated column becomes
    `sha256(convert_to(coalesce(object,''),'UTF8'))` — `sha256()` is core since
    PG 11.
  - `gen_random_uuid()` is core since PG 13.
  Therefore **PG 14 floor** (13 is EOL) and zero extensions.
- **`search_path` names our schema only.** Today every pooled connection runs
  `SET search_path TO "<schema>", public`. As a privileged role that is a
  foot-gun aimed at a neighbour: unqualified DDL can land in `public`, and a
  shadowing object there can resolve ahead of ours. Use
  `options=-csearch_path=<schema>`, no `public`, and fully-qualified identifiers
  in DDL where practical.
- **Nothing instance- or database-wide.** No `CREATE DATABASE`, `ALTER SYSTEM`,
  `ALTER DATABASE … SET`, or role creation. Temp tables are fine — session temp
  schema, gone with the connection.
- **One schema by default:** `ONTOBRICKS_PG_SCHEMA=ontobricks` holds registry and
  graph tables both (names don't collide: `registries`, `domains`,
  `domain_versions`, `domain_permissions`, `app_roles`, `schedules`, … vs
  `g_<dom>_v<n>`, `…_sync`, `…__app`, `ix_*`). `ONTOBRICKS_PG_GRAPH_SCHEMA`
  optionally splits them.
- **Modest connection budget:** other tenants share `max_connections`, so the
  pool defaults small rather than assuming the instance is ours.
- **Schema creation:** `CREATE SCHEMA IF NOT EXISTS` on startup, plus
  `make print-ddl` for DBAs who prefer to pre-create. Startup preflight verifies
  connectivity, PG ≥ 14, and that the schema is usable and writable, reporting
  clearly on failure.

Reference bootstrap DDL:

```sql
CREATE SCHEMA ontobricks AUTHORIZATION ontobricks_app;
GRANT CONNECT ON DATABASE <existing_db> TO ontobricks_app;
GRANT USAGE, CREATE ON SCHEMA ontobricks TO ontobricks_app;
-- TEMP on the database is already granted to PUBLIC by default
```

`CREATE` on the schema is required: graph tables are created per domain-version
at build time, so runtime DDL is inherent to the design.

**PgBouncer.** Transaction-mode pooling breaks per-session `SET` and
cross-transaction temp tables. `options=-c` handles the first; the COPY staging
path already scopes each batch to its own transaction, which handles the second.
A test pins both, because either is easy to regress silently.

## 7. Removals and rename

**Deleted** (~4–5k LOC):

- `back/core/graphdb/neo4j/` (24 src files, 23 test files) and its Settings UI
- `back/core/graphdb/lakebase/SyncedTableManager.py` (1104 LOC, Lakeflow managed-synced)
- `back/core/graphdb/lakebase/provisioner.py` (906 LOC) and `_sync_uc_schema.py`
- `back/core/databricks/lakebase/` (whole package)
- `back/core/databricks/SecretsService.py` (existed for Neo4j)
- `app.yaml.template`, `src/mcp-server/app.yaml*`, `databricks.yml`
- `scripts/deploy.sh`, `scripts/deploy.config.sh`, `scripts/update-deployed-app.sh`,
  `scripts/bootstrap/lakebase-perms.sh`, `scripts/bootstrap/setup-lakebase.sh`,
  `scripts/bootstrap/app-permissions.sh`
- Makefile targets: `deploy`, `deploy-dry-run`, `deploy-volume`, `deploy-no-run`,
  `render-app-yaml`, `bootstrap-perms`, `bootstrap-lakebase`, `bundle-validate`,
  `bundle-summary`, `deploy-check`

**Renamed** — `lakebase` → `postgres`, 54 files / 1134 occurrences:

- `back/core/graphdb/lakebase/` → `back/core/graphdb/postgres/`
- `back/objects/registry/store/lakebase/` → `store/postgres/`
- `LakebaseFlatStore` → `PostgresFlatStore`, `LakebaseBase` → `PostgresBase`,
  `LakebaseRegistryStore` → `PostgresRegistryStore`
- Settings `lakebase_*` → `postgres_*`, with `AliasChoices` keeping the old env
  names working for one release
- `GRAPH_BACKENDS` value `lakebase` → `postgres`, with a normalizer accepting
  the old value so existing `DomainSession.info['graph_backend']` rows keep
  resolving

## 8. Packaging and runtime

- Multi-stage image. Builder: `uv sync --frozen --no-dev` into a venv. Runtime:
  venv + `src/` + `front/static/` + `documentation/` (Help Center serves
  markdown from there at runtime). Non-root user.
- `PORT` (default 8000); `uvicorn` via `run.py`; healthcheck on `/health`, whose
  Apps-mode assertions are reworked (it currently flags "App mode but
  DATABRICKS_CLIENT_ID missing").
- **MCP folded in.** `src/mcp-server` has a hyphen and so isn't importable —
  part of why it is a separate package today. Rename to `src/mcp_server/`, move
  `fastmcp` into the main `pyproject.toml`, delete its `pyproject.toml`,
  `uv.lock`, `app.yaml*` and `deploy-mcp-server.sh`, and mount its
  Streamable-HTTP ASGI app at `/mcp` beside the existing `/api` mount in
  `shared/fastapi/main.py`.
- `docker-compose.yml` (app + postgres) for local dev only; real deployments
  point at an existing instance.
- Secrets by env or pydantic-settings `secrets_dir` (already supported).

## 9. Configuration surface

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | HTTP listen port |
| `DATABASE_URL` | — | Postgres conninfo; or use discrete `PG*` vars |
| `ONTOBRICKS_PG_SCHEMA` | `ontobricks` | Schema holding registry + graph tables |
| `ONTOBRICKS_PG_GRAPH_SCHEMA` | *(unset)* | Optional separate graph schema |
| `ONTOBRICKS_PG_POOL_MIN` / `_MAX` | `1` / `8` | Pool bounds |
| `ONTOBRICKS_AUTH_ENABLED` | `true` | Fail closed; set `false` only for local dev |
| `ONTOBRICKS_OIDC_CLIENT_ID` / `_SECRET` | — | Databricks custom OAuth app integration |
| `ONTOBRICKS_OIDC_REDIRECT_URI` | — | e.g. `https://host/auth/callback` |
| `ONTOBRICKS_BOOTSTRAP_ADMIN` | — | Email seeded as admin on first `initialize()` |
| `DATABRICKS_HOST` | — | Workspace URL (connector + OIDC issuer) |
| `DATABRICKS_CLIENT_ID` / `_SECRET` | — | Service principal for background work |
| `DATABRICKS_SQL_WAREHOUSE_ID` | — | Source reads, Delta engine |
| `ONTOBRICKS_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — | LLM endpoint; Databricks FMAPI is a preset |
| `MLFLOW_TRACKING_URI` | local file store | `databricks` still supported |
| `SECRET_KEY` | — | Session signing |

## 10. Phasing

Each phase ships green on its own. The Apps deploy keeps working as a reference
until P7.

| Phase | Work | Rationale |
|---|---|---|
| P0 | Verify on a real PG 14 that `sha256(convert_to(…))` equals `digest(…,'sha256')`, that `gen_random_uuid()` resolves without pgcrypto, and that the generated-column expression is accepted | Gates the no-extensions design |
| P1 | `RuntimeEnv` split: 37 `is_databricks_app()` + 17 `DATABRICKS_APP_PORT` sites → `PORT` / `ONTOBRICKS_AUTH_ENABLED` / `DatabricksConnector.is_configured()`. No behaviour change on Apps | Riskiest and least visible; do it while old behaviour is still runnable as a reference |
| P2 | `PostgresConnectionPool`; conninfo `search_path`; drop pgcrypto; PG 14 floor; co-tenancy invariant test | The actual Lakebase removal |
| P3 | Rename `lakebase` → `postgres` with `AliasChoices` back-compat | Mechanical; own commit so review is trivial |
| P4 | OIDC login, `IdentitySession`, remove 25 header reads, `app_roles` + `AppRoleService` + admin screen | Depends on P1's auth predicate |
| P5 | Delete Neo4j, `SyncedTableManager`, `provisioner`, `_sync_uc_schema`, `SecretsService` and their Settings UI | Pure subtraction |
| P6 | `engine_base.py` base-URL LLM client + Databricks preset | Independent, small |
| P7 | `src/mcp_server` rename + `/mcp` mount; Dockerfile; compose; Makefile; delete `app.yaml.template`, `databricks.yml`, `scripts/deploy*`; rewrite README and docs | Last, because it removes the reference deploy |

## 11. Testing strategy

Deleted: `tests/units/auth/test_lakebase_auth.py`, ~23 Neo4j test files, and any
test asserting Apps-mode behaviour or header-based identity.

Added:

- **Co-tenancy invariant** (`db`): snapshot `pg_catalog`, install the schema, run
  a full domain build, `DROP SCHEMA … CASCADE`, assert the catalog diff is empty
  — no leftover extensions, types, functions, or objects in `public`.
- **PgBouncer transaction mode** (`db`): the COPY insert/delete path succeeds
  behind a transaction-pooling proxy.
- **OIDC** (`unit`): `state` mismatch rejected, PKCE verifier round-trips,
  refresh-on-401, group extraction from SCIM `/Me`.
- **Fail closed** (`integration`): with `ONTOBRICKS_AUTH_ENABLED=true` and OIDC
  unconfigured, no route serves data.
- **Preflight diagnostics** (`db`): each missing prerequisite is reported by name.
- **PG floor** (`db`): startup refuses PG < 14 with a clear message.

Routine command stays `uv run --frozen pytest -q -m "not scenario"`. `db`- and
`e2e`-marked tests need Docker.

## 12. Risks

1. **The `RuntimeEnv` split failing open.** One mis-split predicate silently
   disables RBAC, because a single boolean currently gates three unrelated
   things. Mitigation: P1 first; the PR enumerates all 54 sites with the intended
   meaning of each; the fail-closed test backstops it.
2. **Per-user UC enforcement changes visibility.** A user who could previously
   read a table *through the SP* may no longer be able to. More correct, but a
   visible behaviour change — needs a release note and probably a config to keep
   SP identity for queries.
3. **Single replica only.** APScheduler runs in-process and sessions are on local
   disk, so two replicas means duplicate scheduled builds. Either document the
   constraint or add Postgres advisory-lock leader election (small).
4. **The 1134-site rename** can break string-keyed config silently. Mitigation:
   separate mechanical commit, `AliasChoices`, and a normalizer accepting the old
   `graph_backend` value.
5. **No Docker on the current dev machine**, so `db`- and `e2e`-marked tests
   cannot be verified locally. P0 and P2 need Docker available.

## 13. Deferred

- Non-Databricks identity provider (generic OIDC).
- Workload identity federation for the service principal.
- Moving attachments off UC Volume (a `BlobStore` seam).
- A local document extractor: `DocumentExtractor` uses `ai_parse_document` on a
  SQL Warehouse, so **document import does not work without the Databricks
  connector**. Known functional gap.
- Multi-replica support (leader election, shared session store).
- Lakebase as an optional `password_provider` plug-in.
