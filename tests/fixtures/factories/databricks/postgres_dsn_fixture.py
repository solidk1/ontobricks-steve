"""Postgres target for ``db``-marked tests.

Resolution order:

1. ``ONTOBRICKS_TEST_DSN`` — a libpq DSN or URL for a real server. Preferred:
   OntoBricks targets Azure Database for PostgreSQL, and testing against the
   actual product (TLS, its PG version, its privilege model) catches things an
   ephemeral local container cannot.
2. ``testcontainers`` — an ephemeral local ``postgres:16-alpine``, when Docker
   is available.
3. Skip, so ``db``-marked tests never gate a PR in an environment with neither.

Every test gets its **own schema** rather than its own database, which mirrors
how OntoBricks installs for real (§6 of the decoupling spec: one new schema in
an existing database, touching nothing outside it). That is what makes running
these against a shared remote server safe, and it lets the residue test assert
the uninstall guarantee directly.

Example::

    export ONTOBRICKS_TEST_DSN="host=pg-x.postgres.database.azure.com \\
        port=5432 dbname=ontobricks user=obadmin sslmode=require"
    export PGPASSWORD=...   # or an Entra access token
    uv run --frozen pytest -m db
"""

from __future__ import annotations

import os
import secrets

import pytest


def _dsn_from_env() -> str:
    return (os.environ.get("ONTOBRICKS_TEST_DSN") or "").strip()


@pytest.fixture(scope="session")
def lakebase_pg(request):
    """Session-scoped Postgres handle with a ``connection_url()``."""
    if os.environ.get("ONTOBRICKS_SKIP_TESTCONTAINERS") == "1" and not _dsn_from_env():
        pytest.skip("ONTOBRICKS_SKIP_TESTCONTAINERS=1 and no ONTOBRICKS_TEST_DSN")

    dsn = _dsn_from_env()
    if dsn:

        class _DsnHandle:
            def connection_url(self) -> str:
                return dsn

            @property
            def is_remote(self) -> bool:
                return True

        yield _DsnHandle()
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip(
            "no ONTOBRICKS_TEST_DSN and testcontainers not installed — "
            "set a DSN or install dev-deps for db-marker tests"
        )

    # Construction itself probes the Docker socket, so it must be inside the
    # guard — not just start().
    try:
        container = PostgresContainer(image="postgres:16-alpine").with_env(
            "POSTGRES_DB", "ontobricks_test"
        )
        container.start()
    except Exception as exc:  # pragma: no cover — Docker absent is environmental
        pytest.skip(
            f"no ONTOBRICKS_TEST_DSN and Docker unavailable ({type(exc).__name__}); "
            "set a DSN to test against a real server instead"
        )

    class _ContainerHandle:
        def connection_url(self) -> str:
            return container.get_connection_url()

        @property
        def is_remote(self) -> bool:
            return False

    request.addfinalizer(container.stop)
    yield _ContainerHandle()


@pytest.fixture
def pg_conn(lakebase_pg):
    """Autocommit connection, or skip when ``psycopg`` is absent."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg required for db tests")
    with psycopg.connect(lakebase_pg.connection_url(), autocommit=True) as conn:
        yield conn


@pytest.fixture
def throwaway_schema(pg_conn):
    """Yield a unique schema name, dropped afterwards.

    A schema rather than a database: it matches how OntoBricks installs, and it
    keeps concurrent runs against one shared server from colliding.
    """
    name = f"ob_test_{secrets.token_hex(6)}"
    yield name
    with pg_conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
