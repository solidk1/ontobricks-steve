"""Tests for get_graph_auth — branch overrides must be inert off Lakebase.

``lakebase_branch`` (``projects/<p>/branches/<b>``) lets the graph store live on
a different Lakebase branch from the registry. An Azure Database for PostgreSQL
server has no branches, and ``BranchLakebaseAuth`` would fail against one — so a
stored branch value from a prior Lakebase deployment must be ignored, not
honoured.

Six call sites used to make this decision inline, each with its own fallback.
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


class TestLakebaseMode:
    def test_branch_override_builds_branch_auth(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "lakebase")
        from back.core.databricks.lakebase import BranchLakebaseAuth

        auth = _get_graph_auth("projects/p/branches/b", "db1")
        assert isinstance(auth, BranchLakebaseAuth)

    def test_no_branch_uses_the_bound_auth(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "lakebase")
        from back.core.databricks.lakebase import BranchLakebaseAuth, LakebaseAuth

        auth = _get_graph_auth("", "")
        assert isinstance(auth, LakebaseAuth)
        assert not isinstance(auth, BranchLakebaseAuth)


class TestEntraMode:
    """The regression this helper exists to prevent."""

    def test_branch_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "entra")
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.databricks.lakebase import BranchLakebaseAuth
        from back.core.postgres import PostgresAuth

        auth = _get_graph_auth("projects/p/branches/b", "db1")
        assert isinstance(auth, PostgresAuth)
        assert not isinstance(auth, BranchLakebaseAuth)

    def test_no_branch_also_yields_postgres_auth(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_PG_AUTH", "entra")
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.postgres import PostgresAuth

        assert isinstance(_get_graph_auth("", ""), PostgresAuth)


class TestAzureHostInference:
    def test_azure_host_ignores_a_stored_branch(self, monkeypatch):
        """No explicit mode: the hostname alone must be enough.

        An operator migrating a Lakebase deployment to Azure changes PGHOST and
        may well leave lakebase_branch behind in the stored engine config.
        """
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        monkeypatch.setenv("PGHOST", "pg-x.postgres.database.azure.com")
        from back.core.databricks.lakebase import BranchLakebaseAuth

        assert not isinstance(_get_graph_auth("projects/p/branches/b", ""), BranchLakebaseAuth)
