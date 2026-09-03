"""Tests for get_graph_auth — one server, addressed by PGHOST.

Lakebase used to be addressed by a control-plane ``projects/<p>/branches/<b>``
path, which let the graph store sit on a different Lakebase branch from the
registry. That concept has no equivalent on any other PostgreSQL server, and a
Lakebase endpoint is now reached exactly like one — via ``PGHOST`` — so the
branch override is retired. A stored value must be ignored, not honoured.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_auth_cache():
    # import_module, because the package re-exports a *class* named
    # LakebaseAuth which shadows the submodule attribute of the same name.
    from importlib import import_module

    mod = import_module("back.core.databricks.lakebase.LakebaseAuth")
    mod._defaults.clear()
    yield
    mod._defaults.clear()


def _get_graph_auth(*args, **kwargs):
    from back.core.databricks import get_graph_auth

    return get_graph_auth(*args, **kwargs)


class TestModeSelection:
    def test_lakebase_mode_yields_lakebase_auth(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "lakebase")
        from back.core.databricks.lakebase import LakebaseAuth

        assert isinstance(_get_graph_auth(), LakebaseAuth)

    def test_entra_mode_yields_postgres_auth(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "entra")
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.postgres import PostgresAuth

        assert isinstance(_get_graph_auth(), PostgresAuth)

    def test_azure_host_infers_entra_without_an_explicit_mode(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.postgres import PostgresAuth

        assert isinstance(_get_graph_auth(), PostgresAuth)

    def test_non_azure_host_stays_on_lakebase(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "ep-abc.database.cloud.databricks.com")
        from back.core.databricks.lakebase import LakebaseAuth
        from back.core.postgres import PostgresAuth

        auth = _get_graph_auth()
        assert isinstance(auth, LakebaseAuth)
        assert not isinstance(auth, PostgresAuth)


class TestBranchOverrideIsRetired:
    def test_branch_path_is_ignored_in_lakebase_mode(self, monkeypatch):
        """The graph store shares the registry server.

        Previously this built a BranchLakebaseAuth pointed at another Lakebase
        project; that class is gone.
        """
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "lakebase")
        from back.core.databricks.lakebase import LakebaseAuth

        plain = _get_graph_auth()
        with_branch = _get_graph_auth("projects/p/branches/b", "db1")
        assert isinstance(with_branch, LakebaseAuth)
        assert with_branch is plain, "both must resolve to the same shared auth"

    def test_branch_path_is_ignored_in_entra_mode(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "entra")
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.postgres import PostgresAuth

        assert isinstance(_get_graph_auth("projects/p/branches/b", ""), PostgresAuth)

    def test_branch_class_no_longer_exists(self):
        """A stored lakebase_branch cannot resurrect the removed code path."""
        import back.core.databricks.lakebase as pkg

        assert not hasattr(pkg, "BranchLakebaseAuth")


class TestRetiredEnvVars:
    def test_lakebase_env_vars_are_not_consulted(self, monkeypatch):
        """LAKEBASE_* is retired: a stale .env must not change resolution."""
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        monkeypatch.setenv("LAKEBASE_PROJECT", "some-project")
        monkeypatch.setenv("LAKEBASE_BRANCH", "some-branch")
        from back.core.postgres import PostgresAuth

        assert isinstance(_get_graph_auth(), PostgresAuth)

    def test_schema_setting_ignores_the_retired_alias(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_PG_SCHEMA", raising=False)
        monkeypatch.setenv("LAKEBASE_SCHEMA", "retired_name")
        from shared.config.settings import Settings

        assert Settings().lakebase_schema == "ontobricks_registry"

    def test_new_schema_var_is_honoured(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_SCHEMA", "chosen")
        from shared.config.settings import Settings

        assert Settings().lakebase_schema == "chosen"
