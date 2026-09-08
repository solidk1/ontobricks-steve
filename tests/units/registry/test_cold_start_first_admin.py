"""First run against a real, empty Postgres: can anyone get in at all?

Three bugs reached a live deployment in a row and the suite was green at 4930 tests
through all of them:

1. Nothing redirected an unauthenticated visitor to ``/auth/login``, so the OIDC
   flow was unreachable without typing the URL.
2. Login depended on a SCIM round-trip, so any SCIM failure blocked sign-in even
   though the access token already carried the identity.
3. ``resolve_role`` deadlocked on a fresh registry: no ``app_roles`` table meant no
   admin, no admin meant no Settings, no Settings meant no table.

They share a shape. Each concerns the *first* interaction with a *fresh* system, and
every existing test asserted what a component returns for given inputs against a
hand-written fake. Number 3 is the sharpest illustration: its fake raised where the
real store catches and returns ``[]``, so eight tests passed on a fix that could not
fire in production.

This module tests the property those all violated, against the **real**
``PostgresRegistryStore`` and a genuinely empty schema: on a brand-new deployment,
the configured bootstrap admin can get in, and nobody else can.

Marked ``db``; runs against ``ONTOBRICKS_TEST_DSN`` or a testcontainer, and skips
otherwise. CI provides ``postgres:16-alpine``.
"""

from __future__ import annotations

import pytest

# Imported for their side effect of registering the fixtures, matching how the
# other db-marked suites pull them in (they are not conftest fixtures).
from tests.fixtures.factories.databricks.postgres_dsn_fixture import (  # noqa: F401
    lakebase_pg,
    pg_conn,
    throwaway_schema,
)

pytestmark = [pytest.mark.db, pytest.mark.integration]

_BOOT = "boot.admin@example.com"
_OTHER = "someone.else@example.com"


@pytest.fixture
def empty_store(pg_conn, throwaway_schema, monkeypatch):
    """A real store pointed at a schema that exists but holds no tables."""
    psycopg = pytest.importorskip("psycopg")
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{throwaway_schema}"')

    from back.objects.registry.store.postgres.store import PostgresRegistryStore

    store = PostgresRegistryStore.__new__(PostgresRegistryStore)
    dsn = pg_conn.info.dsn
    object.__setattr__(store, "_schema", throwaway_schema)
    object.__setattr__(store, "_q", lambda s: f'"{s}"')
    object.__setattr__(
        store, "_connect", lambda: psycopg.connect(dsn, autocommit=True)
    )
    monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", _BOOT)
    return store


class TestTheRealStoreOnAnEmptySchema:
    def test_list_app_roles_returns_empty_rather_than_raising(self, empty_store):
        """The contract every fake in the suite got wrong."""
        assert empty_store.list_app_roles() == []


class TestABrandNewDeploymentLetsTheBootstrapAdminIn:
    def test_bootstrap_admin_resolves_to_admin(self, empty_store):
        """The deadlock, tested against the real store instead of a fake."""
        from back.objects.registry.AppRoleService import AppRoleService

        assert AppRoleService.resolve_role(empty_store, _BOOT) == "admin"

    def test_nobody_else_gets_in(self, empty_store):
        from back.objects.registry.AppRoleService import AppRoleService

        assert AppRoleService.resolve_role(empty_store, _OTHER) == "none"

    def test_a_group_is_not_a_route_to_the_bootstrap_grant(self, empty_store):
        from back.objects.registry.AppRoleService import AppRoleService

        role = AppRoleService.resolve_role(empty_store, _OTHER, groups=[_BOOT, "admins"])
        assert role == "none"

    def test_unset_bootstrap_denies_everyone(self, empty_store, monkeypatch):
        from back.objects.registry.AppRoleService import AppRoleService

        monkeypatch.delenv("ONTOBRICKS_BOOTSTRAP_ADMIN", raising=False)
        assert AppRoleService.resolve_role(empty_store, _BOOT) == "none"


class TestOnceARealAdminExistsTheTableDecides:
    @pytest.fixture
    def store_with_admin(self, empty_store, pg_conn, throwaway_schema):
        with pg_conn.cursor() as cur:
            cur.execute(
                f'''CREATE TABLE "{throwaway_schema}".app_roles (
                       id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                       principal text NOT NULL,
                       principal_type text NOT NULL DEFAULT 'user',
                       display_name text NOT NULL DEFAULT '',
                       role text NOT NULL,
                       created_at timestamptz NOT NULL DEFAULT now(),
                       updated_at timestamptz NOT NULL DEFAULT now(),
                       UNIQUE (principal))'''
            )
            cur.execute(
                f'''INSERT INTO "{throwaway_schema}".app_roles
                    (principal, role) VALUES (%s, 'admin')''',
                (_OTHER,),
            )
        return empty_store

    def test_the_recorded_admin_is_admin(self, store_with_admin):
        from back.objects.registry.AppRoleService import AppRoleService

        assert AppRoleService.resolve_role(store_with_admin, _OTHER) == "admin"

    def test_the_bootstrap_env_var_no_longer_overrides(self, store_with_admin):
        """A deliberate revoke must not be undone by a stale env var."""
        from back.objects.registry.AppRoleService import AppRoleService

        assert AppRoleService.resolve_role(store_with_admin, _BOOT) == "none"
