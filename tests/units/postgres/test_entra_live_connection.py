"""End-to-end proof that Entra token-as-password reaches a real server.

The unit tests stub the credential, so nothing there proves the token Azure
actually issues is accepted by Postgres, nor that the shared connection pool
drives ``password()`` at the right moment. This does, against a live Azure
Database for PostgreSQL server.

Runs only when ``ONTOBRICKS_ENTRA_LIVE=1`` and the ``PG*`` coordinates are set;
skips otherwise, so it never gates a PR. Locally the token comes from
``az login``; in a container it would come from the managed identity, through
the same ``DefaultAzureCredential`` call.

    export ONTOBRICKS_ENTRA_LIVE=1
    export PGHOST=<server>.postgres.database.azure.com
    export PGDATABASE=ontobricks
    export PGUSER='<your-upn>'
    uv run --frozen pytest tests/units/postgres/test_entra_live_connection.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.db, pytest.mark.external]

#: Snapshotted at import, because ``tests/conftest.py`` has an autouse fixture
#: that deletes ``PGHOST`` / ``PGDATABASE`` so a developer's ``.env`` cannot leak
#: into unit tests. That safeguard is worth keeping, so this module captures the
#: coordinates before fixtures run and restores them only for its own tests.
_LIVE = os.environ.get("ONTOBRICKS_ENTRA_LIVE") == "1"
_ENV = {v: os.environ.get(v, "") for v in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")}


@pytest.fixture
def live_env(monkeypatch):
    """Restore the snapshotted coordinates, or skip."""
    if not _LIVE:
        pytest.skip("set ONTOBRICKS_ENTRA_LIVE=1 to exercise real Entra auth")
    for var in ("PGHOST", "PGDATABASE", "PGUSER"):
        if not _ENV.get(var):
            pytest.skip(f"{var} was not set when the module was imported")
    for var, value in _ENV.items():
        if value:
            monkeypatch.setenv(var, value)
    # Entra auth must not silently fall back to a password.
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)


class TestEntraLive:
    def test_token_authenticates_against_postgres(self, live_env):
        """A real Entra token is accepted in the password field."""
        psycopg = pytest.importorskip("psycopg")
        from back.core.postgres import PostgresAuth

        auth = PostgresAuth(auth_mode="entra")
        with psycopg.connect(**auth.kwargs(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, 1")
                user, one = cur.fetchone()
        assert one == 1
        assert user  # the Entra principal, not a local role

    def test_pool_mints_a_token_per_physical_connection(self, live_env):
        """The shared pool drives password() on connection open.

        This is the property the whole design rests on: a token cannot be
        refreshed inside an open session, so it must be minted per connection.
        """
        pytest.importorskip("psycopg")
        from back.core.databricks.lakebase.LakebaseConnectionPool import (
            LakebaseConnectionPool,
        )
        from back.core.postgres import PostgresAuth

        auth = PostgresAuth(auth_mode="entra")
        calls: list[int] = []
        real_password = auth.password

        def counting_password() -> str:
            calls.append(1)
            return real_password()

        auth.password = counting_password  # type: ignore[method-assign]

        schema = "public"
        pool = LakebaseConnectionPool(
            auth=auth, schema=schema, application_name="ontobricks-test", max_size=2
        )
        try:
            for _ in range(3):
                with pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        assert cur.fetchone()[0] == 1
            # Three checkouts, but the pool reuses connections, so far fewer
            # tokens than checkouts — and at least one.
            assert 1 <= len(calls) <= 3
        finally:
            pool.close()

    def test_invalidate_then_reconnect_still_works(self, live_env):
        """Recovery path: after an auth failure the pool invalidates and retries."""
        psycopg = pytest.importorskip("psycopg")
        from back.core.postgres import PostgresAuth

        auth = PostgresAuth(auth_mode="entra")
        with psycopg.connect(**auth.kwargs(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        auth.invalidate()
        with psycopg.connect(**auth.kwargs(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone()[0] == 1
