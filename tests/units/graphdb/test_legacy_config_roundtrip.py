"""The legacy-config migration, against a real database.

`test_legacy_bucket_migration.py` proves `normalize_graph_engine_config` handles
a `lakebase`-keyed config in memory. That is not the same as proving a row
*written* before 0.8 survives a real read-modify-write cycle: the JSON goes
through `global_config`, a store, and psycopg round-tripping.

Requires a Postgres target (`ONTOBRICKS_TEST_DSN`, or Docker for
testcontainers); skips otherwise.
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.factories.databricks.postgres_dsn_fixture import (  # noqa: F401
    lakebase_pg,
    pg_conn,
    throwaway_schema,
)

pytestmark = [pytest.mark.db, pytest.mark.integration]

#: A `graph_engine_config` exactly as 0.7 would have stored it: the bucket keyed
#: `lakebase`, carrying the managed-synced settings and a branch path.
LEGACY_CONFIG = {
    "lakebase": {
        "database": "legacy_db",
        "schema": "legacy_graph",
        "sync_mode": "managed_synced",
        "sync_table_mode": "snapshot",
        "sync_timeout_s": 600,
        "sync_uc_catalog": "main",
        "lakebase_branch": "projects/p/branches/b",
        "lakebase_project": "old-project",
    },
    "neo4j": {"connections": []},
    "lakehouse": {"warehouse_id": "wh-legacy"},
}


def _make_global_config_table(conn, schema: str) -> None:
    """Minimal stand-in for the registry's ``global_config`` row."""
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(
            f'CREATE TABLE "{schema}".global_config ('
            "  id int PRIMARY KEY DEFAULT 1,"
            "  config jsonb NOT NULL DEFAULT '{}'::jsonb)"
        )


class TestLegacyRowSurvivesRoundTrip:
    def test_stored_lakebase_bucket_reads_as_postgres(self, pg_conn, throwaway_schema):
        from back.core.graphdb.engine_config import (
            LEGACY_PG_BUCKET,
            PG_BUCKET,
            postgres_section,
        )

        _make_global_config_table(pg_conn, throwaway_schema)
        with pg_conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{throwaway_schema}".global_config(id, config) '
                "VALUES (1, %s)",
                (json.dumps({"graph_engine_config": LEGACY_CONFIG}),),
            )
            cur.execute(
                f'SELECT config FROM "{throwaway_schema}".global_config WHERE id = 1'
            )
            stored = cur.fetchone()[0]

        raw = stored["graph_engine_config"]
        assert LEGACY_PG_BUCKET in raw, "precondition: the row is 0.7-shaped"

        section = postgres_section(raw)
        assert section["database"] == "legacy_db"
        assert section["schema"] == "legacy_graph"
        assert PG_BUCKET not in raw, "the stored row is untouched by a read"

    def test_rewrite_emits_the_canonical_bucket(self, pg_conn, throwaway_schema):
        """A save after a read must persist `postgres`, not `lakebase`."""
        from back.core.graphdb.engine_config import (
            LEGACY_PG_BUCKET,
            PG_BUCKET,
            normalize_graph_engine_config,
        )

        _make_global_config_table(pg_conn, throwaway_schema)
        with pg_conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{throwaway_schema}".global_config(id, config) '
                "VALUES (1, %s)",
                (json.dumps({"graph_engine_config": LEGACY_CONFIG}),),
            )
            cur.execute(
                f'SELECT config FROM "{throwaway_schema}".global_config WHERE id = 1'
            )
            raw = cur.fetchone()[0]["graph_engine_config"]

            rewritten = normalize_graph_engine_config(raw)
            cur.execute(
                f'UPDATE "{throwaway_schema}".global_config SET config = %s WHERE id = 1',
                (json.dumps({"graph_engine_config": rewritten}),),
            )
            cur.execute(
                f'SELECT config FROM "{throwaway_schema}".global_config WHERE id = 1'
            )
            after = cur.fetchone()[0]["graph_engine_config"]

        assert PG_BUCKET in after
        assert LEGACY_PG_BUCKET not in after, "the legacy key must not be rewritten"
        assert after[PG_BUCKET]["database"] == "legacy_db"
        assert after["lakehouse"]["warehouse_id"] == "wh-legacy"

    def test_retired_keys_still_validate(self, pg_conn, throwaway_schema):
        """The removed managed-synced and branch keys must not fail validation.

        Rejecting them would make a 0.7 config unsaveable, blocking Save in the
        Settings UI on any upgraded deployment.
        """
        from back.core.graphdb.engine_config import postgres_section
        from back.core.graphdb.postgres.PostgresBase import validate_engine_config_keys

        ok, msg = validate_engine_config_keys(postgres_section(LEGACY_CONFIG))
        assert ok is True, msg

    def test_legacy_graph_backend_value_maps(self):
        from back.core.graphdb.GraphDBFactory import normalize_graph_backend

        assert normalize_graph_backend("lakebase") == "postgres"
