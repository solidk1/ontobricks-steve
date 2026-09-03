"""DDL helpers for the bulk table, writable companion table, and union view.

Three Postgres objects per graph version:

- ``g_<dom>_v<n>_sync`` -- bulk data, streamed from the Delta warehouse view
  during a Build and read-only afterwards. The ``_sync`` suffix is historical
  (it once denoted a Lakeflow-owned synced table) and is kept so existing
  deployments need no migration.
- ``g_<dom>_v<n>__app``  -- writable companion (reasoning + cohort writes).
- ``g_<dom>_v<n>``       -- union view that readers query (back-compat name).

Splitting bulk from derived is what lets a rebuild replace the former without
discarding the latter.

The synced table mirrors the source Delta view's columns
(``subject``, ``predicate``, ``object``); the companion carries the full
``(subject, predicate, object, datatype, lang)`` shape used by reasoning
output. The union view casts NULL ``datatype`` / ``lang`` for the synced
side so SPARQL / KG-search readers see a uniform schema.
"""

from __future__ import annotations

from typing import Any

from back.core.helpers import safe_identifier

HASH_FUNCTION_NAME = "sha256_utf8"


def hash_function_ref(schema: str = "") -> str:
    """Qualified reference to the per-schema hash function.

    Unqualified when *schema* is empty: ``search_path`` names only the
    OntoBricks schema (never ``public``), so resolution cannot reach a
    co-tenant's object.
    """
    return f'"{schema}".{HASH_FUNCTION_NAME}' if schema else HASH_FUNCTION_NAME


def _hash_expr(schema: str = "") -> str:
    return f"{hash_function_ref(schema)}(coalesce(object, ''))"


def _triple_table_columns(schema: str = "") -> str:
    return f"""
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            object_hash BYTEA GENERATED ALWAYS AS (
                {_hash_expr(schema)}
            ) STORED,
            datatype TEXT,
            lang TEXT,
            PRIMARY KEY (subject, predicate, object_hash)
"""


_TRIPLE_TABLE_INDEXES: tuple[tuple[str, str], ...] = (
    ("sp", "subject, predicate"),
    ("po", "predicate, object_hash"),
    ("oph", "object_hash, predicate"),
)


def _safe(name: str) -> str:
    return (safe_identifier(name) or "triples").lower()


def synced_phy(name: str) -> str:
    """Postgres table name for the read-only synced table.

    Uses a ``_sync`` suffix (not ``__sync``) so it does not collide with the union
    view identifier (:func:`view_phy`, the legacy reader-facing name).
    """
    return f"{_safe(name)}_sync"


def companion_phy(name: str) -> str:
    """Postgres table name for the writable companion table."""
    return f"{_safe(name)}__app"


def view_phy(name: str) -> str:
    """Postgres view name readers see (matches the legacy single-table name)."""
    return _safe(name)


def _idx_name(table: str, suffix: str) -> str:
    base = f"g_{table}_{suffix}".lower()
    return base[:63]


def ensure_hash_function(cur: Any, schema: str = "") -> None:
    """Create the ``object_hash`` helper, without any extension.

    Replaces ``CREATE EXTENSION pgcrypto``. Two reasons the extension had to
    go: it is *database*-scoped, so it outlives ``DROP SCHEMA ... CASCADE`` and
    is shared with every co-tenant of the database; and on Azure Database for
    PostgreSQL ``CREATE EXTENSION`` is gated behind the **server-level**
    ``azure.extensions`` allowlist, making it an instance-wide change.

    ``sha256()`` has been in core since PG 11, but it cannot be used directly
    in a generated column: ``convert_to()`` is ``STABLE`` and PostgreSQL
    requires generation expressions to be ``IMMUTABLE``, so
    ``sha256(convert_to(object, 'UTF8'))`` is rejected with *"generation
    expression is not immutable"*. This one-line wrapper carries the
    ``IMMUTABLE`` marker instead, which is sound because a given text value's
    UTF-8 encoding is deterministic — ``convert_to``'s ``STABLE`` marking is
    conservative, reflecting that it reads ``server_encoding``, which is fixed
    for the life of a database.

    The function lives inside the OntoBricks schema, so it is removed by
    ``DROP SCHEMA ... CASCADE`` and leaves no residue.
    """
    if schema:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    cur.execute(
        f"CREATE OR REPLACE FUNCTION {hash_function_ref(schema)}(t TEXT) "
        "RETURNS BYTEA LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT "
        "AS $ob$ SELECT sha256(convert_to(t, 'UTF8')) $ob$"
    )




def _table_bare_name(table_ref: str) -> str:
    return table_ref.split(".")[-1].strip('"')


def _table_exists(cur: Any, bare_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = %s
          AND n.nspname = ANY(current_schemas(false))
          AND c.relkind = 'r'
        LIMIT 1
        """,
        (bare_name,),
    )
    return cur.fetchone() is not None


def _has_object_hash_column(cur: Any, bare_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = ANY(current_schemas(false))
          AND table_name = %s
          AND column_name = 'object_hash'
        LIMIT 1
        """,
        (bare_name,),
    )
    return cur.fetchone() is not None


def upgrade_legacy_triple_table_to_object_hash(
    cur: Any, table_ref: str, schema: str = ""
) -> None:
    """Migrate pre-0.6.2 triple tables that still key on full ``object`` text.

    ``CREATE TABLE IF NOT EXISTS`` leaves legacy companions in place; without
    this step ``ensure_graph_indexes`` fails with ``column "object_hash" does
    not exist`` when rebuilding a graph created before 0.6.2.
    """
    bare = _table_bare_name(table_ref)
    if not _table_exists(cur, bare) or _has_object_hash_column(cur, bare):
        return

    ensure_hash_function(cur, schema)

    for sfx, _ in _TRIPLE_TABLE_INDEXES:
        cur.execute(f"DROP INDEX IF EXISTS {_idx_name(bare, sfx)}")

    cur.execute(
        f"ALTER TABLE {table_ref} "
        "ADD COLUMN IF NOT EXISTS object_hash BYTEA GENERATED ALWAYS AS "
        f"({_hash_expr(schema)}) STORED"
    )
    cur.execute(f"ALTER TABLE {table_ref} ADD COLUMN IF NOT EXISTS datatype TEXT")
    cur.execute(f"ALTER TABLE {table_ref} ADD COLUMN IF NOT EXISTS lang TEXT")

    cur.execute(f"""
        DO $$ DECLARE pk_name text;
        BEGIN
          SELECT c.conname INTO pk_name
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
          WHERE c.contype = 'p'
            AND t.relname = {bare!r}
            AND n.nspname = ANY(current_schemas(false))
          LIMIT 1;
          IF pk_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', {bare!r}, pk_name);
          END IF;
        END $$;
        """)
    cur.execute(
        f"ALTER TABLE {table_ref} " "ADD PRIMARY KEY (subject, predicate, object_hash)"
    )


def ensure_graph_indexes(cur: Any, table_ref: str) -> None:
    """Create standard graph lookup indexes on *table_ref* if absent.

    ``table_ref`` may be bare (``g_x_v1_sync``) or schema-qualified
    (``"schema".g_x_v1_sync``). Index names use the physical table name only.
    """
    table_name = table_ref.split(".")[-1].strip('"')
    for sfx, cols in _TRIPLE_TABLE_INDEXES:
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {_idx_name(table_name, sfx)} "
            f"ON {table_ref} ({cols})"
        )


def _create_triple_table(cur: Any, schema: str, table: str) -> None:
    ensure_hash_function(cur, schema)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
        {_triple_table_columns(schema)}
        )
        """)
    upgrade_legacy_triple_table_to_object_hash(cur, table, schema)
    ensure_graph_indexes(cur, table)


def ensure_synced(cur: Any, schema: str, synced: str) -> None:
    """Create the ``_sync`` bulk-data table + standard B-tree indexes if absent.

    Used by the ``app_managed`` build path to provision the table that receives
    warehouse-streamed triples.
    """
    _create_triple_table(cur, schema, synced)


_LAKEFLOW_SYNC_OWNER_PREFIX = "databricks_writer"




def drop_synced(cur: Any, synced: str) -> None:
    """Drop the ``_sync`` bulk-data table (``app_managed`` cleanup path).

    Uses a DO block to skip the DROP when the current session does not own the
    table, which prevents ``must be owner of table`` on a ``_sync`` table left
    behind by a different database role — for example one created by the
    since-removed Lakeflow managed-synced path.
    """
    bare = synced.split(".")[-1].strip('"')
    cur.execute(
        "DO $$ BEGIN "
        "  IF EXISTS ("
        "    SELECT 1 FROM pg_class c "
        "    JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"    WHERE c.relname = {bare!r} "
        "      AND n.nspname = ANY(current_schemas(false)) "
        "      AND c.relkind = 'r' "
        "      AND pg_has_role(session_user, c.relowner, 'MEMBER')"
        "  ) THEN "
        f"    EXECUTE 'DROP TABLE IF EXISTS {synced}'; "
        "  END IF; "
        "END $$"
    )


def ensure_companion(cur: Any, schema: str, companion: str) -> None:
    """Create the writable companion table + standard B-tree indexes if absent."""
    _create_triple_table(cur, schema, companion)


def ensure_union_view(
    cur: Any,
    view: str,
    synced: str,
    companion: str,
) -> None:
    """``CREATE OR REPLACE`` the union view that readers query.

    The synced side is NULL-padded for ``datatype`` / ``lang`` so the view
    has a uniform 5-column shape regardless of which side a row came from.

    If ``view`` already exists as a TABLE (e.g. from an old app-managed build
    before the managed_synced migration), it is dropped first — Postgres's
    ``CREATE OR REPLACE VIEW`` cannot replace a table with a view.
    """
    # Drop any stale TABLE that occupies the view name before (re)creating the view.
    # ``CREATE OR REPLACE VIEW`` cannot replace a table — it only replaces views.
    # We check pg_class using the unqualified name and the current search_path schema
    # so this works regardless of whether the caller schema-qualifies the name.
    bare_name = view.split(".")[-1].strip('"')
    cur.execute(
        "DO $$ BEGIN "
        "  IF EXISTS ("
        "    SELECT 1 FROM pg_class c "
        "    JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"    WHERE c.relname = {bare_name!r} "
        "      AND n.nspname = ANY(current_schemas(false)) "
        "      AND c.relkind = 'r' "
        "      AND pg_has_role(session_user, c.relowner, 'MEMBER')"
        "  ) THEN "
        f"    EXECUTE 'DROP TABLE IF EXISTS {view} CASCADE'; "
        "  END IF; "
        "END $$"
    )
    sql = (
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT subject, predicate, object, "
        f"NULL::TEXT AS datatype, NULL::TEXT AS lang "
        f"FROM {synced} "
        f"UNION ALL "
        f"SELECT subject, predicate, object, datatype, lang FROM {companion}"
    )
    cur.execute(sql)


def truncate_companion(cur: Any, companion: str) -> None:
    """Drop all rows from the companion table (used on full rebuild)."""
    cur.execute(f"TRUNCATE TABLE {companion}")


def drop_companion(cur: Any, companion: str) -> None:
    cur.execute(f"DROP TABLE IF EXISTS {companion}")


def drop_view(cur: Any, view: str) -> None:
    cur.execute(f"DROP VIEW IF EXISTS {view}")
