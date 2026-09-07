# PostgreSQL as Graph DB — OntoBricks Reference

The `postgres` graph engine stores a domain's materialised triples in PostgreSQL.
It is the default engine and works against **any PostgreSQL 14+ server** — Azure
Database for PostgreSQL, RDS, self-hosted, or Databricks Lakebase, which is a
Postgres endpoint like any other.

> Renamed from `lakebase-graphdb.md` in v0.7.1. Lakebase is now one of several
> supported servers rather than the engine's identity, and the `managed_synced`
> write mode it required was removed along with the Databricks Apps deploy.

## Table of contents

1. [Architecture](#1-architecture)
2. [Prerequisites](#2-prerequisites)
3. [Configuring the engine](#3-configuring-the-engine)
4. [Schema layout](#4-schema-layout)
5. [Permissions](#5-permissions)
6. [Building a Knowledge Graph](#6-building-a-knowledge-graph)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Architecture

A Knowledge Graph Build streams R2RML query results out of the source system and
into Postgres. The app owns the write path end to end:

```
Source tables ──R2RML──▶ SQL Warehouse ──fetchmany──▶ OntoBricks
                                                          │
                                          COPY FROM STDIN │
                                                          ▼
                                              Postgres  g_<domain>_v<n>_sync
                                                          ▲
      reasoning / cohort / materialise writes ────────────┘ g_<domain>_v<n>__app
                                                          │
                                       UNION view  g_<domain>_v<n>
```

Reads always target the logical name `g_<domain>_v<n>`, a UNION view over the two
tables. No downstream code — SPARQL translation, graph traversal, analytics —
knows the split exists.

Writes are separated on purpose: bulk warehouse data lands in `*_sync` and is
never modified after a Build, while reasoning and cohort triples go to the
`*__app` companion. A rebuild can therefore replace bulk data without discarding
inferred triples.

> The `_sync` suffix is historical. It once denoted a table owned by a Databricks
> Lakeflow synced-table pipeline (`managed_synced` mode). That mode was removed in
> v0.7.1; the name was kept so existing deployments' objects stay addressable.

---

## 2. Prerequisites

- **PostgreSQL 14+**, reachable on **5432** (not a PgBouncer port — see
  `documentation/deployment.md` §2).
- **`psycopg`**, installed via the `lakebase` extra:
  ```bash
  uv sync --frozen --extra lakebase      # or: pip install '.[lakebase]'
  ```
  The extra is named for historical reasons; it is plain `psycopg[binary]` +
  `psycopg-pool` and is required for every Postgres target.
- **A schema OntoBricks may create objects in** — `USAGE` + `CREATE`. See §5.
- **No extensions.** OntoBricks requires none, deliberately: `CREATE EXTENSION`
  is database- or server-scoped, so it would outlive a `DROP SCHEMA … CASCADE`
  and affect co-tenants. See §4.3.

Connection variables and the `ONTOBRICKS_PG_AUTH` credential modes are documented
once, in `documentation/deployment.md` §2.

---

## 3. Configuring the engine

Per domain, in **Domain → Information → Knowledge Graph**:

| Field | Meaning |
|---|---|
| Engine | `postgres` (default), `databricks`, or `neo4j` |
| Schema | Postgres schema for the triple tables. Empty means "derive it" — see below. |
| Database | Optional. Use a different database on the same server from the registry's. |

Schema resolution precedence (`resolve_postgres_graph_schema`):

1. An explicit `graph_engine_config.schema` always wins.
2. Otherwise the Unity Catalog Volume's schema segment (the middle part of
   `catalog.schema.volume` from **Settings → Registry**), so graph triples land
   under the same namespace as registry artefacts.
3. Otherwise `DEFAULT_GRAPH_SCHEMA` — `ontobricks_graph`.

There is no `ONTOBRICKS_PG_GRAPH_SCHEMA` environment variable; the graph schema is
domain configuration, not deployment configuration.

### Persisted config key

The bucket in `graph_engine_config` is keyed **`postgres`**. Configs written by
0.7 and earlier used `lakebase`; those are read transparently and rewritten to the
canonical key on the next save, so no migration step is needed. The retired
`sync_mode`, `sync_uc_catalog` and `lakebase_branch` keys still validate and are
ignored.

---

## 4. Schema layout

### 4.1 Schemas

| Schema | Default | Created by | Contents |
|---|---|---|---|
| Registry | `ontobricks_registry` (`ONTOBRICKS_PG_SCHEMA`) | **Settings → Registry → Initialize** | Domains, versions, permissions, schedules, app roles, global config |
| Graph | `ontobricks_graph` | First Knowledge Graph Build | Per-domain triple tables and views |

They may be the same schema, or in different databases on the same server.

### 4.2 Objects per graph version

| Object | Naming | Written by | Contents |
|---|---|---|---|
| Bulk table | `g_<domain>_v<n>_sync` | Build, via `COPY FROM STDIN` | Warehouse triples: `(subject, predicate, object, datatype, lang)` |
| Companion | `g_<domain>_v<n>__app` | Reasoning, cohorts, materialise | Inferred / derived triples, same columns |
| UNION view | `g_<domain>_v<n>` | App DDL | `SELECT … FROM _sync UNION ALL SELECT … FROM __app` |

`PostgresFlatStore.drop_table(name)` removes all three in dependency order: the
view, then the companion, then the bulk table.

### 4.3 Long literals — `object_hash`, and why there is no `pgcrypto`

Postgres btree indexes cannot index TEXT longer than ~2704 bytes, so uniqueness is
keyed on a hash rather than the literal:

```sql
object_hash BYTEA GENERATED ALWAYS AS (<schema>.sha256_utf8(coalesce(object, ''))) STORED,
PRIMARY KEY (subject, predicate, object_hash)
```

The full `object` is still stored for reads and deletes.

`sha256_utf8()` is a small **in-schema** function OntoBricks creates itself:

```sql
CREATE OR REPLACE FUNCTION <schema>.sha256_utf8(t TEXT)
RETURNS BYTEA LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $ob$ SELECT sha256(convert_to(t, 'UTF8')) $ob$
```

Two reasons it exists rather than `pgcrypto`'s `digest()`:

1. **Co-tenancy.** `CREATE EXTENSION` is database-scoped and survives
   `DROP SCHEMA … CASCADE`. On Azure it is additionally gated behind the
   server-level `azure.extensions` allowlist, making it an instance-wide change
   affecting every co-tenant.
2. **`convert_to` is `STABLE`, not `IMMUTABLE`.** A generated column requires an
   `IMMUTABLE` expression, so `sha256(convert_to(...))` inline is rejected with
   *"generation expression is not immutable"*. The wrapper is declared
   `IMMUTABLE`, which is sound here because the byte encoding of a given text
   value never changes. This was found against a real PG 16.15 server — reading
   the documentation did not predict it.

Indexes created alongside: `(predicate, object_hash)` and `(object_hash, predicate)`.

Graphs created before the `object_hash` layout are upgraded in place by
`upgrade_legacy_triple_table_to_object_hash`; without it `ensure_graph_indexes`
fails with *column "object_hash" does not exist*.

---

## 5. Permissions

The application role needs, on the graph schema:

```sql
GRANT USAGE, CREATE ON SCHEMA <graph_schema> TO <app_role>;
```

`CREATE` is required because the Build creates per-version tables, the view, the
indexes, and the `sha256_utf8()` function. On tables it already created the role
is owner, so no further grants are needed.

If a DBA pre-creates the schema rather than letting the app do it:

```sql
CREATE SCHEMA <graph_schema> AUTHORIZATION <app_role>;
```

Nothing needs superuser. On Azure, `azure_pg_admin` is sufficient — and it is not
a superuser, which is precisely why the no-extensions design matters.

The previous `scripts/bootstrap/lakebase-perms.sh` was removed in v0.7.1; the two
statements above replace it.

---

## 6. Building a Knowledge Graph

1. **Settings → Registry → Initialize** — creates the registry schema and tables.
2. Create a domain, import source metadata, design the ontology.
3. Map entities and relationships (**Mapping**) so R2RML has something to run.
4. **Build** — R2RML runs on the warehouse, rows stream back in `fetchmany`
   batches, and `COPY FROM STDIN` + `INSERT … ON CONFLICT DO NOTHING` load the
   `*_sync` table. The companion and UNION view are created on first Build.
5. Query via **Graph Viewer**, SPARQL, GraphQL, or the MCP tools.

Reasoning and cohort operations write to `*__app` and are immediately visible
through the UNION view without a rebuild.

---

## 7. Troubleshooting

**`prepared statement "..." already exists`.** You are connected through PgBouncer
in transaction mode, which conflicts with psycopg3's `prepare_threshold`. Use port
5432.

**`generation expression is not immutable` during Initialize or Build.** The
in-schema `sha256_utf8()` wrapper is missing, usually because the schema was
created by a pre-v0.7.1 version. Re-run Initialize.

**`column "object_hash" does not exist`.** A legacy triple table predates the
hash layout and the upgrade step has not run. Re-run the Build, or drop and
rebuild the graph.

**`permission denied for schema`.** The role lacks `CREATE`. See §5 — the Build
creates objects, so read-only access is not enough.

**Auth failures every ~30–60 minutes on Azure.** An Entra token expired without
refreshing. Tokens are minted per physical connection with a 300s margin; an
external pooler in front of the app breaks that assumption.

**`connection refused` after idle.** Databricks Lakebase scales to zero when idle;
the pool retries cold starts. Other servers may be briefly unreachable during
failover — the same retry covers it.

**Triples missing after reasoning.** Check the companion table directly:
`SELECT count(*) FROM <schema>.g_<domain>_v<n>__app`. If the count is right but
queries disagree, the UNION view is stale or missing — drop and rebuild the graph.

**`DROP SCHEMA` leaves objects behind.** It should not; that is the co-tenancy
invariant, and `tests/units/graphdb/test_pg_cotenancy.py` asserts it against a
real server. If you see residue, that is a bug worth reporting.
