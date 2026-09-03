"""Tests for PostgresAuth and the auth-mode selector."""

from __future__ import annotations

import pytest

from back.core.errors import ValidationError
from back.core.postgres import AUTH_ENTRA, AUTH_PASSWORD, PostgresAuth

pytestmark = pytest.mark.unit


class _StubCredential:
    def __init__(self):
        self.invalidated = 0

    def token(self):
        return "entra-token"

    def invalidate(self):
        self.invalidated += 1


@pytest.fixture
def pg_env(monkeypatch):
    monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "ontobricks")
    monkeypatch.setenv("PGUSER", "app@tenant")
    monkeypatch.delenv("PGSSLMODE", raising=False)
    monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)


class TestMode:
    def test_defaults_to_entra(self, pg_env):
        assert PostgresAuth().auth_mode == AUTH_ENTRA

    def test_env_selects_password(self, pg_env, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "password")
        assert PostgresAuth().auth_mode == AUTH_PASSWORD

    def test_unknown_mode_is_rejected(self, pg_env, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "kerberos")
        with pytest.raises(ValidationError, match="ONTOBRICKS_PG_AUTH"):
            PostgresAuth()


class TestCoordinates:
    def test_kwargs_shape_matches_what_the_pool_consumes(self, pg_env):
        auth = PostgresAuth(credential=_StubCredential())
        kw = auth.kwargs(application_name="ontobricks-graphdb")
        assert kw["host"] == "pg-x.postgres.database.azure.com"
        assert kw["port"] == 5432
        assert kw["dbname"] == "ontobricks"
        assert kw["user"] == "app@tenant"
        assert kw["password"] == "entra-token"
        assert kw["application_name"] == "ontobricks-graphdb"

    def test_tls_is_required_by_default(self, pg_env):
        assert PostgresAuth(credential=_StubCredential()).sslmode == "require"

    def test_sslmode_can_be_tightened(self, pg_env, monkeypatch):
        monkeypatch.setenv("PGSSLMODE", "verify-full")
        assert PostgresAuth(credential=_StubCredential()).sslmode == "verify-full"

    def test_keepalives_are_set(self, pg_env):
        """Without these a dead pooled connection stalls the next query ~130s."""
        kw = PostgresAuth(credential=_StubCredential()).kwargs()
        assert kw["keepalives"] == 1
        assert kw["keepalives_idle"] == 10

    @pytest.mark.parametrize(
        "missing,expected",
        [("PGHOST", "PGHOST"), ("PGDATABASE", "PGDATABASE"), ("PGUSER", "PGUSER")],
    )
    def test_missing_coordinates_are_named(
        self, pg_env, monkeypatch, missing, expected
    ):
        monkeypatch.delenv(missing, raising=False)
        auth = PostgresAuth(credential=_StubCredential())
        with pytest.raises(ValidationError, match=expected):
            auth.kwargs()

    def test_non_numeric_port_is_rejected(self, pg_env, monkeypatch):
        monkeypatch.setenv("PGPORT", "five-four-three-two")
        auth = PostgresAuth(credential=_StubCredential())
        with pytest.raises(ValidationError, match="PGPORT"):
            _ = auth.port


class TestPassword:
    def test_entra_mode_uses_the_credential(self, pg_env):
        stub = _StubCredential()
        assert PostgresAuth(credential=stub).password() == "entra-token"

    def test_password_mode_uses_pgpassword(self, pg_env, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "password")
        monkeypatch.setenv("PGPASSWORD", "s3cret")
        assert PostgresAuth().password() == "s3cret"

    def test_password_mode_without_pgpassword_is_rejected(self, pg_env, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "password")
        monkeypatch.delenv("PGPASSWORD", raising=False)
        with pytest.raises(ValidationError, match="PGPASSWORD"):
            PostgresAuth().password()

    def test_invalidate_delegates_to_the_credential(self, pg_env):
        stub = _StubCredential()
        PostgresAuth(credential=stub).invalidate()
        assert stub.invalidated == 1

    def test_invalidate_is_safe_in_password_mode(self, pg_env, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "password")
        PostgresAuth().invalidate()  # must not raise


class TestSelector:
    """The selector must not disturb existing Lakebase deployments."""

    def _resolve(self):
        from back.core.databricks.lakebase.LakebaseAuth import resolve_pg_auth_mode

        return resolve_pg_auth_mode()

    def test_azure_host_infers_entra(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        assert self._resolve() == "entra"

    def test_non_azure_host_stays_on_lakebase(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "ep-abc.database.us-west-2.cloud.databricks.com")
        assert self._resolve() == "lakebase"

    def test_unset_host_stays_on_lakebase(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.delenv("PGHOST", raising=False)
        assert self._resolve() == "lakebase"

    def test_explicit_setting_overrides_host_inference(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "lakebase")
        assert self._resolve() == "lakebase"
