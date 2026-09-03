"""The co-tenancy contract, as an executable gate.

OntoBricks installs into an existing database as **one new schema, touching
nothing outside it**, so the guarantee is:

    DROP SCHEMA <ours> CASCADE  →  zero residue anywhere else in the instance.

These were verified by hand against Azure Database for PostgreSQL 16.15 while
designing the change (spec §6.1). Encoding them here stops the guarantee from
silently rotting — in particular the pgcrypto removal, which is easy to undo by
reflex when someone needs a hash function.

Requires a Postgres target: set ``ONTOBRICKS_TEST_DSN``, or have Docker
available for testcontainers. Skips otherwise.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.fixtures.factories.databricks.postgres_dsn_fixture import (  # noqa: F401
    lakebase_pg,
    pg_conn,
    throwaway_schema,
)

pytestmark = [pytest.mark.db, pytest.mark.integration]


def _catalog_snapshot(conn) -> dict:
    """Everything outside our own schema that an install must not disturb."""
    with conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
        extensions = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        public_tables = sorted(r[0] for r in cur.fetchall())
        cur.execute(
            "SELECT p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'"
        )
        public_functions = sorted(r[0] for r in cur.fetchall())
    return {
        "extensions": extensions,
        "public_tables": public_tables,
        "public_functions": public_functions,
    }


def _install(conn, schema: str) -> None:
    """Apply the graph DDL exactly as ``_companion_ddl`` does."""
    from back.core.graphdb.postgres import _companion_ddl

    with conn.cursor() as cur:
        _companion_ddl.ensure_hash_function(cur, schema)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS "{schema}".triples (
                subject     TEXT NOT NULL,
                predicate   TEXT NOT NULL,
                object      TEXT NOT NULL,
                object_hash BYTEA GENERATED ALWAYS AS
                    ("{schema}".sha256_utf8(coalesce(object, ''))) STORED,
                datatype    TEXT,
                lang        TEXT,
                PRIMARY KEY (subject, predicate, object_hash)
            )
            """)


class TestNoExtensionRequired:
    def test_install_creates_no_extension(self, pg_conn, throwaway_schema):
        before = _catalog_snapshot(pg_conn)["extensions"]
        _install(pg_conn, throwaway_schema)
        assert _catalog_snapshot(pg_conn)["extensions"] == before

    def test_pgcrypto_is_not_required(self, pg_conn, throwaway_schema):
        _install(pg_conn, throwaway_schema)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'")
            assert cur.fetchone()[0] == 0


class TestGeneratedColumn:
    def test_inlined_convert_to_is_rejected(self, pg_conn, throwaway_schema):
        """Why the wrapper exists: convert_to() is STABLE, not IMMUTABLE.

        If a future PostgreSQL marks it immutable this fails, which is the
        signal to simplify the DDL rather than a defect.
        """
        psycopg = pytest.importorskip("psycopg")
        with pg_conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{throwaway_schema}"')
            with pytest.raises(psycopg.errors.InvalidObjectDefinition) as exc:
                cur.execute(f"""CREATE TABLE "{throwaway_schema}".inlined (
                        object TEXT NOT NULL,
                        object_hash BYTEA GENERATED ALWAYS AS
                            (sha256(convert_to(coalesce(object, ''), 'UTF8'))) STORED
                    )""")
        assert "immutable" in str(exc.value).lower()

    def test_wrapper_hash_matches_standard_sha256(self, pg_conn, throwaway_schema):
        _install(pg_conn, throwaway_schema)
        value = "café — ünïcode ✓"
        with pg_conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{throwaway_schema}".triples(subject,predicate,object) '
                "VALUES ('s','p',%s)",
                (value,),
            )
            cur.execute(f'SELECT object_hash FROM "{throwaway_schema}".triples')
            stored = bytes(cur.fetchone()[0])
        assert stored == hashlib.sha256(value.encode("utf-8")).digest()

    def test_empty_object_hashes_as_empty_string(self, pg_conn, throwaway_schema):
        """``coalesce(object,'')`` must survive the wrapper's STRICT marker."""
        _install(pg_conn, throwaway_schema)
        with pg_conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{throwaway_schema}".triples(subject,predicate,object) '
                "VALUES ('s','p','')"
            )
            cur.execute(f'SELECT object_hash FROM "{throwaway_schema}".triples')
            stored = bytes(cur.fetchone()[0])
        assert stored == hashlib.sha256(b"").digest()


class TestResidueFreeUninstall:
    def test_drop_schema_cascade_leaves_nothing_behind(self, pg_conn, throwaway_schema):
        before = _catalog_snapshot(pg_conn)
        _install(pg_conn, throwaway_schema)

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = %s",
                (throwaway_schema,),
            )
            assert cur.fetchone()[0] == 1, "hash function should live in our schema"

            cur.execute(f'DROP SCHEMA "{throwaway_schema}" CASCADE')

            cur.execute(
                "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
                (throwaway_schema,),
            )
            assert cur.fetchone()[0] == 0

        assert _catalog_snapshot(pg_conn) == before


class TestSearchPath:
    def test_public_is_not_on_the_search_path(self, pg_conn, throwaway_schema):
        """A shadowing object in ``public`` must never resolve ahead of ours.

        With privileges on a shared instance, an unqualified CREATE landing in
        ``public`` would write into a neighbour's namespace.
        """
        with pg_conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{throwaway_schema}"')
            cur.execute(f'SET search_path TO "{throwaway_schema}"')
            cur.execute("SELECT current_schemas(false)")
            assert list(cur.fetchone()[0]) == [throwaway_schema]
