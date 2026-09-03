<p align="center">
  <img src="src/front/static/global/img/ontobricks-icon.svg" alt="OntoBricks Logo" width="120" height="120">
</p>

<h1 align="center">OntoBricks 0.7.1</h1>

<p align="center">
  <strong>Knowledge Graph Builder for Databricks</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/fastapi-0.109+-green.svg" alt="FastAPI">
</p>

## Project Description

OntoBricks is a web application that transforms Databricks tables into a materialized graph viewer. It lets you design ontologies (OWL), map them to Unity Catalog tables via R2RML, materialize triples into a Delta-backed triple store and a Lakebase Postgres graph engine, reason over the graph (OWL 2 RL, SWRL, SHACL), and query it through an auto-generated GraphQL API. The entire pipeline — from metadata import to a queryable graph viewer — can run in four clicks using LLM-powered automation.

## Project Support

Please note that all projects in the /databrickslabs github account are provided for your exploration only, and are not formally supported by Databricks with Service Level Agreements (SLAs). They are provided AS-IS and we do not make any guarantees of any kind. Please do not submit a support ticket relating to any issues arising from the use of these projects.

Any issues discovered through the use of this project should be filed as GitHub Issues on the Repo. They will be reviewed as time permits, but there are no formal SLAs for support.

## Building the Project

OntoBricks uses [uv](https://docs.astral.sh/uv/) for dependency management. All dependencies are declared in `pyproject.toml`.

```bash
# Clone the repository
git clone <repository-url>
cd OntoBricks

# Install dependencies (uv resolves them from pyproject.toml)
uv sync

# Or use the setup script
scripts/setup.sh
```

### Prerequisites

- Python 3.10 or higher.
- **PostgreSQL 14 or newer.** OntoBricks installs into an *existing* database
  as **one new schema** and touches nothing outside it, so
  `DROP SCHEMA <schema> CASCADE` uninstalls it completely. **No extensions are
  required.** Azure Database for PostgreSQL (Flexible Server) is the reference
  target; Databricks Lakebase and any other PostgreSQL server work too.
- *Optional* — a **Databricks workspace**, for the features that genuinely need
  one: reading Unity Catalog tables as mapping sources, the Delta triple-store
  engine, UC Volume attachments, Lakeview dashboard listing,
  `ai_parse_document` document import, and the Foundation Model API. OntoBricks
  starts and runs without it; those features report themselves unavailable.
- *Optional* — a **SQL Warehouse**, if you use the Databricks connector.

## Deploying / Installing the Project

OntoBricks is an ordinary Python web application: one process, configured
entirely by environment variables. There is no platform-specific bundle and no
generated manifest. See `.env.example` for the full contract.

### Database setup (once, by a DBA)

```sql
CREATE SCHEMA ontobricks AUTHORIZATION ontobricks_app;
GRANT CONNECT ON DATABASE <existing_db> TO ontobricks_app;
GRANT USAGE, CREATE ON SCHEMA ontobricks TO ontobricks_app;
-- TEMP on the database is already granted to PUBLIC by default
```

`CREATE` on the schema is required, not optional: graph tables are created per
domain-version at build time, so the app performs DDL at runtime. Nothing
outside that schema is ever touched.

For Microsoft Entra authentication, also map the identity to a Postgres role:

```sql
SELECT * FROM pgaadauth_create_principal('<managed-identity-or-upn>', false, false);
```

### Local development

```bash
cp .env.example .env
# Set PGHOST / PGDATABASE / PGUSER, and ONTOBRICKS_AUTH_ENABLED=false
make dev            # http://127.0.0.1:8000, auto-reload
```

`ONTOBRICKS_AUTH_ENABLED` defaults to **true** (fail closed), so a deployment
that configures nothing is locked rather than served unauthenticated. Turn it
off explicitly for local work.

### Running in a container

```bash
ONTOBRICKS_CONTAINERIZED=true \
PORT=8000 \
PGHOST=<server> PGDATABASE=<db> PGUSER=<principal> \
ONTOBRICKS_PG_AUTH=entra \
ONTOBRICKS_AUTH_ENABLED=true \
ONTOBRICKS_OIDC_CLIENT_ID=... ONTOBRICKS_OIDC_REDIRECT_URI=https://<host>/auth/callback \
ONTOBRICKS_BOOTSTRAP_ADMIN=you@example.com \
python run.py
```

`ONTOBRICKS_CONTAINERIZED=true` is what makes uvicorn bind `0.0.0.0` instead of
loopback and puts sessions and logs under `/tmp`. Without it the process binds
`127.0.0.1` and nothing outside the container can reach it.

Two things the image must contain: `documentation/` (the Help Center serves
markdown from it at runtime) and, for `PGSSLMODE=verify-full`, the Azure root CA.

> **Single replica.** APScheduler runs in-process and sessions are on local
> disk, so run exactly one instance. More than one duplicates every scheduled
> build; scaling to zero stops the scheduler entirely. On Azure Container Apps
> that means `minReplicas: 1, maxReplicas: 1` — its defaults violate both.

### Authentication

Off the Databricks Apps platform there are no proxy identity headers, so
OntoBricks runs the OAuth 2.0 authorization-code + PKCE flow itself against a
Databricks **custom OAuth app integration** (account console → App
connections), registered with `https://<host>/auth/callback` as its redirect
URI. One login yields the user's email, their groups, and a Databricks user
token — the last enabling per-user Unity Catalog enforcement on interactive
queries.

App-level access lives in the registry's `app_roles` table, not in a Databricks
App ACL. `ONTOBRICKS_BOOTSTRAP_ADMIN` seeds the first admin, without which a
fresh deployment would have nobody able to grant access; it applies only while
no admin exists, so a deliberate revoke is not undone. Manage grants from
**Settings → App access**, or via `GET`/`POST /settings/app-roles{,/grant,/revoke}`.
The last admin cannot be revoked.

### Graph analytics job (optional)

`resources/graph_analytics.job.yml` defines the serverless job that computes
large-graph metrics. It is no longer deployed by this repository — create it in
your workspace with your own asset bundle or the Jobs UI, then set
`ONTOBRICKS_ANALYTICS_JOB_ENABLED=true`. Below the in-memory triple cap the
metrics are computed in-process and the job is not needed.

## Testing

- **Routine / CI:** `uv run --frozen pytest -q -m "not scenario"` — the fast in-process suite (the opt-in live scenarios are excluded). Keep `--frozen`: a bare `uv run` re-resolves dependencies against the internal pypi proxy and rewrites `uv.lock`, which breaks the next deploy.
- **Live scenario campaign:** `make scenario-campaign` — an ordered, billable end-to-end journey (import → generate → collaborate → rules/analysis → validate) against a **running** app, writing reports to `artifacts/scenarios/`. See [`tests/e2e/scenarios/README.md`](tests/e2e/scenarios/README.md) for env vars, chaining, isolation, and how to add a scenario.

## Releasing the Project

1. Ensure all tests pass: `make test`
2. Update the version in `pyproject.toml`
3. Commit, tag, and push:

```bash
git add -A && git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

4. Roll out the new version with whatever deploys your container.

## Using the Project

### Automated Pipeline (4 clicks)

| Step | Action | What Happens |
|------|--------|--------------|
| **1** | **Import Metadata** (Domain > Metadata) | Fetches table and column metadata from Unity Catalog |
| **2** | **Generate Ontology** (Ontology > Wizard) | LLM designs entities, relationships, and attributes from your metadata |
| **3** | **Auto-Map** (Mapping > Auto-Map) | LLM generates SQL mappings for every entity and relationship |
| **4** | **Synchronize** (Knowledge Graph > Status) | Executes mappings and populates the triple store |

### Domain & registry (0.1.2 UX)

- **Ontology Designer** — the main ontology graph view lives under **Ontology → Designer** (visual canvas + AI Assistant).
- **Version lifecycle (DRAFT / IN-REVIEW / PUBLISHED)** — every domain version carries a lifecycle status, shown as a colour-coded badge across the navbar, Domain Information, Registry Browse, Domain Versions and query headers. Only **DRAFT** versions are editable; the external API/GraphQL/MCP only serve the **numeric-latest PUBLISHED** version. Transitions (DRAFT ↔ IN-REVIEW → PUBLISHED, PUBLISHED → DRAFT admin-only) are enforced server-side and replace the former "Active"/`mcp_enabled` toggle.
- **Single-editor concurrency** — a DRAFT version is editable by **one user at a time**. The first opener edits; anyone else opening the same version is automatically **read-only** with a banner naming the editor. The lock uses a **renew-only lease**: the editor's browser keeps it alive in the background while the domain is open (a hover **countdown tooltip** on the navbar domain badge shows the remaining time), so an active session never expires, but a lock left behind by a crashed/abandoned tab **auto-releases** once its lease lapses (`ONTOBRICKS_EDIT_LOCK_TTL_S`, default 10 min; `0` disables → hold-until-close). It is also released immediately when the editor clicks **Close** (which prompts *Save before closing?*, releases the lock and returns to Home) or an **app-admin takes over**. **Opening a different domain closes the current one first** — the previous domain's lock is released server-side *before* the new one loads, so you never hold two locks; a same-domain **version** switch releases the old version's lock after the new one loads. The **Save** and **Close** buttons sit together in the domain sub-navigation. Admins also get a registry-wide **Settings → Locks** panel listing every active lock (domain, version, status, holder, acquired time, staleness) with a **force-unlock** action.
- **Domain Cockpit (Validation)** — **Published Version** shows which registry version is exposed via **API / MCP**; it can differ from the version you have loaded in the editor.
- **Registry → Browse** — drives the **lifecycle status transitions** for a domain's versions; **Domain → Versions** shows that status as a read-only badge.
- **Validation & Review workflow** — a business-user-oriented review layer on top of the lifecycle. **Home → My Tasks** is a cross-domain worklist of versions waiting on you (submit, sign off, or publish). **Domain → Validation** shows a soft consistency-check summary, a reviewer sign-off panel and the full audit trail. Submit-for-review and Publish stay builder/admin (Publish unlocks for a builder once the **sign-off quorum** is met — a **per-domain** setting, default 1, editable on **Domain → Information → Global** — while an **admin can publish at any time, overriding the quorum**, with the override flagged in the audit trail); **sign-off** (approve / request-changes) is open to any domain member, and request-changes reopens the version to DRAFT. Every decision (with `from → to` status snapshots) is persisted append-only in the `domain_review_events` registry table.
- **New domain** — after **New Domain**, a full-page loading overlay runs until Domain Information finishes its first load.
- **Domain Information** — triple-store / snapshot / local graph paths update when you **commit** the domain name (blur or change) or change version (aligned with naming rules before save).
- **Duplicate names** — **Save to Unity Catalog** is blocked if the sanitized domain name already exists in the registry (inline check + confirmation before POST).
- **Navbar** — domain name and version in the top bar refresh after load, save, clear, import, and version switches (browser cache invalidated on those actions).

### Graph DB engine (per-domain — Domain → Information → Knowledge Graph)

The **graph** triple-store backend is pluggable (`GraphDBFactory` / `GraphDBBackend`). The **selection is made per domain** — each domain picks its backend from a single **Graph Backend** dropdown under **Domain → Information → Knowledge Graph** (mandatory; defaults to `lakebase`). Three engines ship:

- **Lakebase (Postgres)** — default; **three Postgres objects per domain version** (`*_sync` bulk-data table, `*__app` companion for reasoning/cohort writes, `g_<dom>_v<n>` UNION view for reads) inside a configurable Postgres schema on the **App-bound** Lakebase database (same connection as the optional Lakebase registry backend). Requires the `lakebase` extra (`uv sync --extra lakebase`) so `psycopg` is installed.
- **Lakehouse** — governed Unity Catalog Delta triple tables; no separate graph database to provision.
- **Neo4j** — native graph database over Bolt (Neo4j Aura or self-hosted). The `neo4j` Python driver is a core dependency.

The backend *selection* is stored per-domain in `DomainSession.info['graph_backend']` and versioned with the domain. Switching a domain's backend after a build requires **rebuilding** the Knowledge Graph — graph artifacts are not migrated between engines.

Engine *connection* config is **workspace-global**, under **Settings → Back end** (PostgreSQL / Lakehouse / Neo4j sections), and stored as JSON in `graph_engine_config`. For PostgreSQL the supported keys are **`database`** (optional override of `PGDATABASE`) and **`schema`** (optional, default `ontobricks_graph`). The bucket is keyed `postgres`; a config written before 0.8 is keyed `lakebase` and is still read, then rewritten canonically on the next save. Keys from the removed Lakeflow managed-synced mode (`sync_mode`, `sync_table_mode`, `sync_timeout_s`, `sync_uc_catalog`, `sync_uc_schema`) are accepted and ignored, so an old config still validates.

> **Schema grants.** The connecting principal needs `USAGE + CREATE` on each
> schema OntoBricks touches — the registry schema (`ONTOBRICKS_PG_SCHEMA`) and,
> if you point the graph engine at a different one, the graph schema
> (`graph_engine_config.schema`, default `ontobricks_graph`). Apply them with
> the `GRANT` statements shown under *Database setup*, or use
> **Settings → Registry → Repair permissions**, which runs the equivalent
> in-app when the connecting role owns the schema.

> **Build performance.** When the active engine is PostgreSQL, the Knowledge Graph build streams warehouse rows in `fetchmany` batches (`SQLWarehouse.iter_rows`) and ingests them via `COPY FROM STDIN` into a per-batch temp table followed by `INSERT … ON CONFLICT DO NOTHING` (and the symmetrical `DELETE … USING` for incremental removes). The FastAPI process never holds the full graph or the full diff: snapshot CTAS and `EXCEPT` execution stay warehouse-side, the app pipes one batch at a time. There is no Volume archive thread — Postgres is the system of record for the graph.

> **Graph layout.** Each graph version is three Postgres objects: `g_<dom>_v<n>_sync`
> (bulk triples streamed from the warehouse during a Build), `g_<dom>_v<n>__app`
> (writable companion holding reasoning and cohort writes), and `g_<dom>_v<n>`
> (UNION view over both, carrying the legacy single-table name so readers are
> unaffected). Splitting bulk from derived is what lets a rebuild replace the
> former without discarding the latter. The `_sync` suffix is historical — it
> once denoted a table owned by a Databricks Lakeflow pipeline; that
> `managed_synced` mode was removed, and the suffix is kept so existing
> deployments need no migration.

### Manual Workflow

1. **Design** an ontology visually using the OntoViz canvas, or import OWL/RDFS/industry standards (FIBO, CDISC, IOF, HL7 FHIR R4/R4B/R5)
2. **Map** ontology entities to Databricks tables with column-level precision
3. **Build** the Knowledge Graph — materializes triples into the triple store (incremental by default)
4. **Query** through the GraphQL playground or explore the interactive graph viewer
5. **Reason** over the graph — run OWL 2 RL inference, SWRL rules, SHACL validation, and constraint checks

### Graph Viewer Features

- **Two-phase search** — preview matching entities in a flat list, then select specific ones to expand into the full graph with relationships and neighbors
- **Configurable search depth** — control the maximum traversal depth and entity cap for graph expansion
- **Right-click "Expand neighbours"** — enrich the current graph in place with N-hop neighbours of any selected node (depth follows the right-pane Depth slider, default 2); newly added entities are highlighted and the camera zooms to frame them, with a non-blocking spinner in the canvas top-right while the request runs
- **Bridge navigation** — follow cross-domain bridges to automatically switch domains and focus on the target entity in the graph viewer
- **Class actions (Unity Catalog functions)** — bind any number of UC functions to an ontology class (**Ontology → Designer → External**), then run them on a node from the details pane or its right-click menu and read the result in a popup. Each function takes exactly one argument, the entity's ID, and only functions declared on the node's class can be invoked. The same actions are exposed to the MCP server via `get_entity_context` and `invoke_entity_action`. See [`documentation/user-guide.md`](documentation/user-guide.md#class-actions-unity-catalog-functions).
- **Data cluster detection** — detect communities in the graph viewer using Louvain, Label Propagation, or Greedy Modularity algorithms; available client-side (Graphology) for the visible subgraph and server-side (NetworkX) for the full graph; cluster results can be visualized with color-by-cluster mode and collapsed into super-nodes
- **Cohort discovery** — group entities that travel together using rule-based linkage (shared resources via predicates) and compatibility constraints (same-value, value-equals, value-in, value-range); deterministic, explainable cohorts with live counters, why/why-not explainers, and idempotent materialisation as graph triples (`:inCohort`) or Unity Catalog Delta tables. See [`documentation/cohort_discovery.md`](documentation/cohort_discovery.md).
- **Data quality violation limits** — cap the number of violations displayed per rule (configurable via dropdown, default 10) for faster quality checks
- **Per-rule progress tracking** — SWRL inference and data quality checks report progress for each individual rule

### AI Assistant

The **Ontology Designer** view (**Ontology → Designer**) includes a floating AI Assistant (bottom-right of the canvas) that lets you modify your ontology through natural language commands — add entities, remove orphans, list relationships, and more. Conversation history is maintained within the session.

### Navigation & Performance

- **Deep-linked sidebar sections** — shareable URLs, browser Back/Forward support
- **Breadcrumb navigation** — always see your position (Registry > Domain > Ontology > Section)
- **Keyboard shortcuts** — `Cmd/Ctrl+S` save, `Cmd/Ctrl+K` search, `?` help overlay
- **SQL connection pooling** — reusable database connections, no per-query TLS handshake
- **CSRF protection** — double-submit cookie for all state-changing requests
- **Structured JSON logging** — set `LOG_FORMAT=json` for production-grade observability

### MCP Integration

OntoBricks exposes the graph viewer to LLM agents via the [Model Context Protocol](https://modelcontextprotocol.io/). Deploy the companion `mcp-ontobricks` app and connect from Cursor, Claude Desktop, or the Databricks Playground.

### Registry OBX Export / Import (UI)

Export one or more domains directly from **Registry → Browse** to a portable
`.obx` file with per-domain version-mode selection (Latest / Active / All /
Choose). Import with per-domain conflict resolution (Skip / Overwrite / Rename).
No command line required — ideal for ad-hoc transfers and cross-tenant sharing.

### Registry Import / Export (CLI)

For automated promotion pipelines use the
`scripts/registry_transfer.sh` command-line tool — export a curated subset
of domains/versions from a source registry into a `.zip`, then preview and
commit it into the target registry. See
[Registry Import / Export](documentation/import-export.md) for the full reference,
examples, and a comparison of the OBX UI vs CLI approaches.

### Ontology Pitfalls Detector

Detect 19 structural, logical, and semantic pitfalls (P1.1–P4.7) in your
ontology from the **Ontology → Pitfalls** sidebar panel. Fast graph-only
checks run immediately; ML-heavy checks (semantic similarity, NLP naming)
require installing the optional extra:

```bash
uv sync --extra pitfalls
```

### Documentation

Full documentation is available in [`documentation/`](documentation/README.md). For a comprehensive feature list and architecture details, see [INFO.md](documentation/INFO.md).
