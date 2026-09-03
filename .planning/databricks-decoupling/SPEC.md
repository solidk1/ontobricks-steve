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
| Engines | Postgres + Delta + **Neo4j** — all three retained (revised 2026-09-01; Neo4j was to be deleted) |
| Registry | **Azure Database for PostgreSQL — Flexible Server**, PG ≥ 14 |
| App-level roles | New `app_roles` table + `ONTOBRICKS_BOOTSTRAP_ADMIN` |
| Auth default | `ONTOBRICKS_AUTH_ENABLED` defaults **on** (fail closed) |
| Attachments | UC Volume, unchanged |
| LLM | OpenAI-compatible base URL; Databricks FMAPI as a preset |
| Postgres auth | Microsoft Entra ID via `DefaultAzureCredential`: token-as-password, minted per physical connection |
| Postgres connection | Direct port 5432 (no PgBouncer) |
| Artifact | One container image, MCP mounted at `/mcp`, compose for local dev |
| Postgres install | One new schema in an existing database, no extensions, residue-free uninstall |
| Container host | Azure Container Apps, pinned to a single replica |

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
  `PGUSER` / `PGSSLMODE`. No `PGPASSWORD` on the Azure path — see below.
- **Implemented as `EntraCredential` + `PostgresAuth`** in `back/core/postgres/`,
  verified against the live server (§6.1). The pool is *reused*, not rewritten:
  its `auth.kwargs()` / `auth.invalidate()` contract and its
  retry-once-on-auth-failure loop are already exactly what token rotation needs.

- **`password_provider` is required, not optional.** Entra ID authenticates by
  presenting an access token *as the password*; tokens live 5–60 minutes and
  cannot be refreshed inside an open session, so a fresh token must be minted for
  every new **physical** connection. `psycopg_pool` supports this via a
  connection-creation hook. This is the one idea carried over from
  `LakebaseAuth`, and Azure turns it from a convenience into a requirement.
- Token acquisition uses `DefaultAzureCredential` (`azure-identity`), scope
  `https://ossrdbms-aad.database.windows.net/.default`. One call resolves both
  the Container Apps **managed identity** in a deployment and the developer's
  `az login` session locally, so there is a single code path and no password
  fallback. `PGUSER` is the Entra principal name, mapped once with
  `pgaadauth_create_principal`.
- `search_path` stays a session-level `SET` on each new connection, which is
  correct on a direct 5432 connection — see §6 for why it must *not* move into
  the conninfo.
- Pool bounds default to min 1 / max 8, both configurable. Azure caps
  `max_connections` by SKU (a B1ms allows roughly 35) and co-tenants share that
  budget, so the default does not assume the server is ours.

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

- **No database-scoped objects, so no `pgcrypto`.** On Azure this is stronger
  than a preference: `CREATE EXTENSION` is gated behind the **server-level**
  `azure.extensions` allowlist, so enabling pgcrypto is an instance-wide change
  affecting every co-tenant — and it outlives the schema drop. Both functions it
  provided are in core Postgres:
  - `gen_random_uuid()` is core since PG 13.
  - `sha256()` is core since PG 11 — but it **cannot be used directly** in the
    `object_hash` generated column. `convert_to()` is marked `STABLE`, and
    PostgreSQL requires generation expressions to be `IMMUTABLE`, so
    `sha256(convert_to(coalesce(object,''),'UTF8'))` is rejected with
    *"generation expression is not immutable"* (verified — see §6.1).

    The fix is a one-line `IMMUTABLE` wrapper **inside our own schema**, so it
    drops with the schema and keeps the invariant:

    ```sql
    CREATE OR REPLACE FUNCTION <schema>.sha256_utf8(t TEXT) RETURNS BYTEA
      LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
      AS $$ SELECT sha256(convert_to(t, 'UTF8')) $$;

    object_hash BYTEA GENERATED ALWAYS AS
      (<schema>.sha256_utf8(coalesce(object,''))) STORED
    ```

    Marking it `IMMUTABLE` is sound: a given text value's UTF-8 byte encoding is
    deterministic. `convert_to`'s `STABLE` marking is conservative because it
    reads `server_encoding`, which is fixed for the life of a database.
  Therefore **PG 14 floor** (13 is EOL) and zero extensions.
- **`search_path` names our schema only.** Today every pooled connection runs
  `SET search_path TO "<schema>", public`. As a privileged role that is a
  foot-gun aimed at a neighbour: unqualified DDL can land in `public`, and a
  shadowing object there can resolve ahead of ours. Keep the session-level `SET`
  (correct on a direct 5432 connection), drop `, public`, and prefer
  fully-qualified identifiers in DDL.

  **Do not** move this into the conninfo as `options=-csearch_path=…`. Azure's
  built-in PgBouncer may reject the `options` startup packet, and the usual
  remedy (`pgbouncer.ignore_startup_parameters`) makes it *ignore* the parameter
  — `search_path` would then be silently unset, and since generated SQL relies
  on it (`GraphDBBackend.py:135`) unqualified DDL could resolve into a
  co-tenant's schema. That is exactly the hazard this contract exists to prevent.
- **Nothing may require superuser.** `azure_pg_admin` is deliberately not a true
  superuser on Flexible Server. This design needs none, but
  `LakebaseFlatStore.py:571` tells the user to "connect to Lakebase with a
  superuser" — unreachable advice on Azure. P2 rewords it to name the schema
  owner.
- **TLS is mandatory.** Azure requires SSL: `sslmode=require` at minimum,
  `verify-full` with the Azure root CA shipped in the image.
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

**PgBouncer is out of scope** (direct 5432 only), but the reasons are recorded
because Azure's built-in pooler is one server parameter away and someone will
reach for it when connection counts bite. Transaction-mode pooling breaks
per-session `SET` and cross-transaction temp tables, and Azure ships
`pgbouncer.max_prepared_statements=0` while psycopg3 defaults
`prepare_threshold=5`. Moving to 6432 therefore needs `SET LOCAL search_path`
per transaction and `prepare_threshold=None`. The COPY staging path already
scopes each batch to its own transaction, so that part is already compatible.

### 6.1 P0 verification (done, 2026-09-01)

Verified against a real target rather than reasoned about — this is why P0 gates
the design.

**Target:** `pg-sshao-ontobricks.postgres.database.azure.com`, PostgreSQL
**16.15**, `Standard_B1ms`, westus2, resource group `rg-sshao-ontobricks`,
subscription `azure-sandbox-field-eng`, Entra + password auth both enabled,
firewall limited to the dev host. Tagged `RemoveAfter=2026-10-01`. Server
encoding `UTF8`; `pgcrypto` absent.

| Check | Result |
|---|---|
| `pg_proc.provolatile` for `sha256` | `i` (immutable) — usable in a generated column |
| `pg_proc.provolatile` for `convert_to` | **`s` (stable)** — *not* usable in a generated column |
| Generated column using `sha256(convert_to(…))` | **rejected**: "generation expression is not immutable" |
| Generated column via `IMMUTABLE` wrapper in-schema | accepted, together with the composite PK on `object_hash` |
| Hash correctness | `p0test.sha256_utf8('café — ünïcode ✓')` equals `shasum -a 256` of the same UTF-8 bytes, byte for byte |
| `gen_random_uuid()` with no `pgcrypto` | works |
| `DROP SCHEMA … CASCADE` residue | schema, table **and function** gone; `public` still empty; no extensions beyond built-in `plpgsql` |

**Verdict:** the no-extensions design holds, with the wrapper-function
correction. The residue-free invariant is demonstrated, not assumed.

## 7. Removals and rename

**Deleted** (~4–5k LOC):

- `back/core/graphdb/lakebase/SyncedTableManager.py` (1104 LOC, Lakeflow managed-synced)
- `back/core/graphdb/lakebase/provisioner.py` (906 LOC) and `_sync_uc_schema.py`
- `back/core/databricks/lakebase/LakebaseAuth.py` + `LakebaseConnectionPool.py`
  (replaced in P2 by `back/core/postgres/`)

**Retained**, contrary to the original plan:

- `back/core/graphdb/neo4j/` — the Neo4j engine stays. Its removal would have
  touched ~60 files (not the 24+23 first estimated): `DigitalTwin`,
  `_build_pipeline`, `Domain`, `DomainSession`, `GlobalConfigService`, the SWRL
  Cypher translator, the Help Center and `menu_config.json`.
- `back/core/databricks/SecretsService.py` — it exists to back the Neo4j
  "Databricks secret" auth flow (Settings → Back end → Neo4j), so it survives
  with Neo4j. Connector-gated: without the Databricks connector, Neo4j
  credentials come from the environment instead.
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
- Secrets by env or pydantic-settings `secrets_dir` (already supported). On the
  Azure path there is no DB secret at all — the managed identity supplies it.

**Single replica is a hard constraint, and Container Apps' defaults violate
it.** APScheduler runs in-process and sessions live on local disk, so the
container app must pin `minReplicas: 1, maxReplicas: 1`:

- `maxReplicas > 1` duplicates every scheduled build and splits sessions across
  replicas.
- `minReplicas: 0` (scale-to-zero, which Container Apps allows) stops the
  scheduler, so scheduled builds silently never run.

Session state is ephemeral, so a revision restart signs users out; with OIDC that
costs one redirect. Lifting the constraint needs advisory-lock leader election
plus a shared session store — deferred (§13).

## 9. Configuration surface

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | HTTP listen port |
| `DATABASE_URL` | — | Postgres conninfo; or the discrete `PG*` vars |
| `PGHOST` | — | `<server>.postgres.database.azure.com` |
| `PGUSER` | — | Entra principal name (see `pgaadauth_create_principal`) |
| `PGSSLMODE` | `require` | Azure mandates TLS; prefer `verify-full` |
| `ONTOBRICKS_PG_AUTH` | `entra` | `entra` (token-as-password) or `password` |
| `AZURE_CLIENT_ID` | — | Only for a *user-assigned* managed identity |
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
| ~~P0~~ | **Done** (§6.1). Found that `convert_to` is `STABLE`, so the generated column needs an `IMMUTABLE` in-schema wrapper; everything else confirmed on PG 16.15 | Gated the no-extensions design |
| P1 | `RuntimeEnv` split: 37 `is_databricks_app()` + 17 `DATABRICKS_APP_PORT` sites → `PORT` / `ONTOBRICKS_AUTH_ENABLED` / `DatabricksConnector.is_configured()`. No behaviour change on Apps | Riskiest and least visible; do it while old behaviour is still runnable as a reference |
| P2a | **Done.** No-extension schema DDL (in-schema `sha256_utf8` wrapper), `search_path` without `public`, superuser remediation reworded, co-tenancy gate tests | Azure-compatible storage |
| P2b | **Done.** `back/core/postgres/` — `EntraCredential` (token-as-password, per-connection minting) + `PostgresAuth`, selected by `resolve_pg_auth_mode()`. Reuses the existing pool rather than rewriting it | The app can now reach Azure Postgres |
| P2c | Retire `BranchLakebaseAuth` and the Lakebase branch/project config once P5b lands | Deferred behind P5b |
| P3 | Rename `lakebase` → `postgres` with `AliasChoices` back-compat | Mechanical; own commit so review is trivial |
| P4 | OIDC login, `IdentitySession`, remove 25 header reads, `app_roles` + `AppRoleService` + admin screen | Depends on P1's auth predicate |
| P5 | Delete `SyncedTableManager`, `provisioner`, `_sync_uc_schema`, `grants.py` and the Settings UI driving them. Neo4j and `SecretsService` are **kept** | Pure subtraction; shrinks P2's surface |
| P6 | `engine_base.py` base-URL LLM client + Databricks preset | Independent, small |
| P7 | `src/mcp_server` rename + `/mcp` mount; Dockerfile; compose; Makefile; delete `app.yaml.template`, `databricks.yml`, `scripts/deploy*`; rewrite README and docs | Last, because it removes the reference deploy |

## 11. Testing strategy

Deleted: `tests/units/auth/test_lakebase_auth.py` and any test asserting
Apps-mode behaviour, header-based identity, Lakeflow managed-synced mode, or
Lakebase provisioning. Neo4j tests are retained.

Added:

- **Co-tenancy invariant** (`db`): snapshot `pg_catalog`, install the schema, run
  a full domain build, `DROP SCHEMA … CASCADE`, assert the catalog diff is empty
  — no leftover extensions, types, functions, or objects in `public`.
- **Entra token minting** (`unit`): `password_provider` is called once per new
  physical connection, never reused past expiry, and a token failure surfaces as
  `InfrastructureError` rather than a bare psycopg error.
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
3. **Single replica only**, and Container Apps' defaults break it: autoscaling
   duplicates scheduled builds, scale-to-zero stops them entirely. Pinning
   `minReplicas: 1, maxReplicas: 1` is a deployment *requirement*, not a
   footnote.
4. **The 1134-site rename** can break string-keyed config silently. Mitigation:
   separate mechanical commit, `AliasChoices`, and a normalizer accepting the old
   `graph_backend` value.
5. **`db`-marked tests now have a real target** (§6.1), so P2 is verifiable.
   Docker is still absent, so `testcontainers`-based tests must either be
   repointed at `ONTOBRICKS_TEST_DSN` or skipped; CI needs its own instance or
   Docker. `e2e` (Playwright) remains unrun — it needs a live server and
   browsers.
6. **`sslmode=verify-full` needs the Azure root CA** in the image. Defaulting to
   `require` is weaker than it should be, so the Dockerfile carries the cert and
   the docs recommend `verify-full`.

## 13. Deferred

- Non-Databricks identity provider (generic OIDC).
- Workload identity federation for the service principal.
- Moving attachments off UC Volume (a `BlobStore` seam).
- A local document extractor: `DocumentExtractor` uses `ai_parse_document` on a
  SQL Warehouse, so **document import does not work without the Databricks
  connector**. Known functional gap.
- Multi-replica support (leader election, shared session store).
- Further `password_provider` plug-ins (Lakebase, AWS RDS IAM, Cloud SQL) — the
  seam is built for Entra and these reuse it.
- PgBouncer (port 6432): needs `SET LOCAL search_path` per transaction and
  `prepare_threshold=None`, because Azure ships
  `pgbouncer.max_prepared_statements=0` while psycopg3 defaults
  `prepare_threshold=5`, giving intermittent "prepared statement does not
  exist" errors.
