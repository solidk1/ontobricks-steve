"""Azure China and US Gov are different clouds, not different regions.

Why this file exists
--------------------
Two values were hardcoded to the global cloud, and both fail in ways that point at
the wrong culprit:

1. ``OSSRDBMS_SCOPE`` — the Entra audience for Azure Database for PostgreSQL. It
   is a fixed per-cloud constant, not a pattern on the server name. Request the
   global audience against Azure China and the token is rejected at connection
   time, which reads as a credential problem.

2. ``AZURE_PG_SUFFIX`` — used to *infer* the auth mode from ``PGHOST``. A
   ``*.postgres.database.chinacloudapi.cn`` host matched nothing, so the inference
   fell through to ``lakebase``: presenting a Databricks Lakebase JWT to Azure
   Postgres. Also reads as a credential problem.

Both are now derived from one table keyed on the host suffix, so a sovereign
deployment needs no configuration beyond ``PGHOST`` — and the two cannot drift
apart, which is why the inference imports the table rather than keeping its own
copy.

These are unit tests against a table of constants. **They cannot prove the
audience values are correct** — that needs a real connection in each cloud, which
is not available here. What they do prove is that the plumbing selects per cloud
instead of assuming one, and that the override exists for when a value is wrong.
"""

from __future__ import annotations

import pytest

from back.core.postgres.EntraCredential import (
    ENV_TOKEN_SCOPE,
    OSSRDBMS_SCOPE,
    PG_SCOPE_BY_HOST_SUFFIX,
    EntraCredential,
    resolve_pg_token_scope,
)

pytestmark = pytest.mark.unit

_HOSTS = {
    "global": "ob.postgres.database.azure.com",
    "china": "ob.postgres.database.chinacloudapi.cn",
    "usgov": "ob.postgres.database.usgovcloudapi.net",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(ENV_TOKEN_SCOPE, raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    yield monkeypatch


class TestScopeSelection:
    @pytest.mark.parametrize("cloud,host", _HOSTS.items())
    def test_each_cloud_resolves_to_a_known_audience(self, cloud, host):
        assert resolve_pg_token_scope(host) in PG_SCOPE_BY_HOST_SUFFIX.values()

    @pytest.mark.parametrize("cloud,host", _HOSTS.items())
    def test_every_audience_is_a_well_formed_scope(self, cloud, host):
        scope = resolve_pg_token_scope(host)
        assert scope.startswith("https://") and scope.endswith("/.default")

    def test_the_audience_is_not_a_pattern_on_the_host(self):
        """Recording *why* this is a lookup table rather than string surgery.

        The global cloud's server is ``*.postgres.database.azure.com`` and its
        audience is ``ossrdbms-aad.database.windows.net`` — different domains
        entirely. An earlier version of this test asserted the host's domain
        appeared in its audience and failed for exactly this reason; the
        assertion was wrong, not the code.
        """
        global_host = _HOSTS["global"]
        assert "azure.com" not in resolve_pg_token_scope(global_host)

    def test_the_three_audiences_are_distinct(self):
        """A copy-paste error here is invisible until a connection is attempted."""
        assert len(set(PG_SCOPE_BY_HOST_SUFFIX.values())) == len(PG_SCOPE_BY_HOST_SUFFIX)

    def test_china_does_not_get_the_global_audience(self):
        assert resolve_pg_token_scope(_HOSTS["china"]) != OSSRDBMS_SCOPE

    def test_unrecognised_host_falls_back_to_global(self):
        """Usually a self-hosted server behind a CNAME, where the operator has set
        the scope explicitly or is not using Entra at all."""
        assert resolve_pg_token_scope("pg.internal.example") == OSSRDBMS_SCOPE

    def test_pghost_is_read_when_no_host_is_passed(self, clean_env):
        clean_env.setenv("PGHOST", _HOSTS["china"])
        assert resolve_pg_token_scope() == PG_SCOPE_BY_HOST_SUFFIX[
            ".postgres.database.chinacloudapi.cn"
        ]

    def test_case_and_whitespace_do_not_defeat_matching(self, clean_env):
        clean_env.setenv("PGHOST", f"  {_HOSTS['china'].upper()}  ")
        assert resolve_pg_token_scope() != OSSRDBMS_SCOPE


class TestOverride:
    def test_explicit_scope_wins(self, clean_env):
        clean_env.setenv(ENV_TOKEN_SCOPE, "https://custom.example/.default")
        clean_env.setenv("PGHOST", _HOSTS["china"])
        assert resolve_pg_token_scope() == "https://custom.example/.default"

    def test_blank_override_is_ignored(self, clean_env):
        """An empty variable is unset, not a request for an empty audience."""
        clean_env.setenv(ENV_TOKEN_SCOPE, "   ")
        clean_env.setenv("PGHOST", _HOSTS["global"])
        assert resolve_pg_token_scope() == OSSRDBMS_SCOPE


class TestCredentialUsesTheResolvedScope:
    def test_scope_comes_from_pghost_at_construction(self, clean_env):
        clean_env.setenv("PGHOST", _HOSTS["china"])
        assert EntraCredential()._scope == PG_SCOPE_BY_HOST_SUFFIX[
            ".postgres.database.chinacloudapi.cn"
        ]

    def test_explicit_scope_argument_still_wins(self, clean_env):
        clean_env.setenv("PGHOST", _HOSTS["china"])
        assert EntraCredential(scope="https://x/.default")._scope == "https://x/.default"

    def test_no_pghost_gives_the_global_default(self):
        assert EntraCredential()._scope == OSSRDBMS_SCOPE


class TestAuthModeInference:
    """The second half: a sovereign host must not be read as Lakebase."""

    def _mode(self, monkeypatch, host):
        monkeypatch.setenv("PGHOST", host)
        monkeypatch.delenv("ONTOBRICKS_PG_AUTH", raising=False)
        from back.core.databricks.lakebase.LakebaseAuth import resolve_pg_auth_mode

        return resolve_pg_auth_mode()

    @pytest.mark.parametrize("cloud,host", _HOSTS.items())
    def test_azure_postgres_infers_entra_in_every_cloud(self, clean_env, cloud, host):
        assert self._mode(clean_env, host) == "entra", (
            f"{cloud} host inferred the wrong auth mode; a Lakebase JWT against "
            "Azure Postgres fails as if the credential were bad"
        )

    def test_non_azure_host_still_infers_lakebase(self, clean_env):
        """Existing Lakebase deployments must be unaffected."""
        assert self._mode(clean_env, "instance.cloud.databricks.com") == "lakebase"

    def test_explicit_setting_always_wins(self, clean_env):
        clean_env.setenv("PGHOST", _HOSTS["china"])
        clean_env.setenv("ONTOBRICKS_PG_AUTH", "password")
        from back.core.databricks.lakebase.LakebaseAuth import resolve_pg_auth_mode

        assert resolve_pg_auth_mode() == "password"

    def test_inference_and_scope_share_one_table(self):
        """Two copies of the suffix list would drift, and the symptom would be a
        host that authenticates with the wrong audience."""
        from back.core.databricks.lakebase.LakebaseAuth import _azure_pg_suffixes

        assert set(_azure_pg_suffixes()) == set(PG_SCOPE_BY_HOST_SUFFIX)
