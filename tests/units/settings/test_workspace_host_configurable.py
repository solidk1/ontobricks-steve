"""The workspace host is settable from the UI; the login issuer is not.

Why this file exists
--------------------
``DATABRICKS_HOST`` was displayed read-only ("Set via environment variable"), so
correcting it meant a redeploy. That mattered because the wrong value is easy to
pick and fails silently: an account console — or a vanity domain in front of one
— answers OIDC discovery perfectly and returns **HTTP 303** for every
``/api/2.0/...`` path, so login works while Unity Catalog and SQL warehouse
calls quietly redirect into nothing.

Two invariants follow, and both are asserted here:

1. Saving probes the host and **rejects** one that redirects, naming the
   account-vs-workspace mistake rather than reporting a generic failure.
2. The OIDC issuer stays an environment variable. It is needed to authenticate
   before anyone can reach this setting, so a wrong value stored behind its own
   login would be unrecoverable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.errors import ValidationError
from back.objects.domain.SettingsService import SettingsService

pytestmark = pytest.mark.unit

_ACCOUNT = "https://oneenvazure.azuredatabricks.net"
_WORKSPACE = "https://adb-7405611364794897.17.azuredatabricks.net"


def _resp(status: int):
    return MagicMock(status_code=status)


class TestProbeRejectsAccountHosts:
    """A redirect is the account-host signature; 401 is a real workspace."""

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_redirect_is_rejected(self, status):
        with patch("requests.get", return_value=_resp(status)):
            ok, msg = SettingsService.probe_workspace_host(_ACCOUNT)
        assert ok is False
        assert str(status) in msg

    def test_rejection_names_the_actual_mistake(self):
        with patch("requests.get", return_value=_resp(303)):
            _, msg = SettingsService.probe_workspace_host(_ACCOUNT)
        assert "account console" in msg
        assert "adb-" in msg, "must show the per-workspace URL shape"
        assert "?o=" in msg, "must say where to find the workspace id"

    @pytest.mark.parametrize("status", [401, 403])
    def test_unauthorized_is_a_pass(self, status):
        """401 means a real workspace API answered and wants credentials."""
        with patch("requests.get", return_value=_resp(status)):
            ok, _ = SettingsService.probe_workspace_host(_WORKSPACE)
        assert ok is True

    def test_unreachable_host_is_not_fatal(self):
        """A private-endpoint workspace may be unreachable from here but fine
        from the app, so this warns rather than blocking the save."""
        with patch("requests.get", side_effect=OSError("no route")):
            ok, msg = SettingsService.probe_workspace_host(_WORKSPACE)
        assert ok is True
        assert "could not be reached" in msg

    def test_server_error_is_not_fatal(self):
        with patch("requests.get", return_value=_resp(503)):
            ok, msg = SettingsService.probe_workspace_host(_WORKSPACE)
        assert ok is True
        assert "503" in msg


class TestSetWorkspaceHost:
    def _ctx(self):
        domain = MagicMock()
        domain.databricks = {}
        return domain

    def _call(self, value, *, probe_ok=True, saved=True):
        domain = self._ctx()
        with patch.object(SettingsService, "require_admin_error"), patch.object(
            SettingsService,
            "probe_workspace_host",
            return_value=(probe_ok, "probe note"),
        ), patch.object(
            SettingsService,
            "_resolve_context",
            return_value=(domain, "old", "tok", {"postgres_schema": "obreg"}),
        ), patch(
            "back.objects.session.global_config_service.set_workspace_host",
            return_value=(saved, "save note"),
        ):
            return SettingsService.set_workspace_host(
                value, "admin@example.com", "utok", MagicMock(), MagicMock()
            ), domain

    def test_requires_admin(self):
        with patch.object(
            SettingsService, "require_admin_error", side_effect=ValidationError("nope")
        ):
            with pytest.raises(ValidationError):
                SettingsService.set_workspace_host(
                    _WORKSPACE, "user@example.com", "t", MagicMock(), MagicMock()
                )

    def test_a_failing_probe_blocks_the_save(self):
        with pytest.raises(ValidationError):
            self._call(_ACCOUNT, probe_ok=False)

    def test_saves_globally(self):
        out, _ = self._call(_WORKSPACE)
        assert out["scope"] == "global"
        assert out["host"] == _WORKSPACE

    def test_also_applies_to_the_session(self):
        _, domain = self._call(_WORKSPACE)
        assert domain.databricks["host"] == _WORKSPACE

    def test_falls_back_to_session_when_the_registry_write_fails(self):
        out, domain = self._call(_WORKSPACE, saved=False)
        assert out["scope"] == "session"
        assert domain.databricks["host"] == _WORKSPACE

    def test_bare_hostname_gets_https(self):
        out, _ = self._call("adb-1.17.azuredatabricks.net")
        assert out["host"].startswith("https://")

    def test_trailing_slash_is_stripped(self):
        out, _ = self._call(_WORKSPACE + "/")
        assert out["host"] == _WORKSPACE

    def test_empty_clears_the_override(self):
        out, domain = self._call("")
        assert out["host"] == ""
        assert "host" not in domain.databricks
        assert "DATABRICKS_HOST" in out["message"]

    def test_empty_does_not_probe(self):
        """Clearing the override must not fail on an unreachable empty host."""
        with patch.object(SettingsService, "probe_workspace_host") as probe:
            self._call("")
        probe.assert_not_called()


class TestOIDCIssuerIsNotSettableHere:
    """The login issuer must not be reachable through this endpoint."""

    def test_the_service_never_writes_the_oidc_host(self):
        out, domain = TestSetWorkspaceHost()._call(_WORKSPACE)
        assert "ONTOBRICKS_OIDC_HOST" not in str(out)
        assert not any("oidc" in k.lower() for k in domain.databricks)

    def test_no_route_exposes_the_oidc_host(self):
        """A grep-level guard: nothing should accept it as request input."""
        from pathlib import Path

        router = Path("src/api/routers/internal/settings.py").read_text()
        assert 'data.get("oidc_host")' not in router
        assert 'data.get("issuer")' not in router
