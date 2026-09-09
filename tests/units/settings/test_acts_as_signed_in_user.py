"""Interactive Unity Catalog calls run as the signed-in user.

Why this file exists
--------------------
The OIDC login requests the ``all-apis`` scope and keeps the resulting Databricks
access token precisely so workspace calls can be made as the caller — the
``OIDCClient`` docstring calls this "per-user Unity Catalog enforcement".

But ``fetch_warehouses`` and the UC browsing helpers never received it. They
built a client from the *session or environment* token only, so on a deployment
with no service principal they returned "Databricks not configured" while every
signed-in user was holding a token that would have worked. The visible effect was
an empty SQL Warehouse picker on a page the user had just authenticated to.

Two properties are asserted: the token reaches the client, and it takes
precedence over the app's own identity so Unity Catalog applies the caller's
grants rather than the service principal's.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.helpers.DatabricksHelpers import DatabricksHelpers

pytestmark = pytest.mark.unit

_USER_TOKEN = "user-oauth-token"
_ENV_TOKEN = "service-principal-token"


@pytest.fixture
def no_global_host():
    """Isolate from the registry: the global host lookup is not under test."""
    with patch.object(DatabricksHelpers, "_global_workspace_host", return_value=""):
        yield


def _settings(token=""):
    s = MagicMock()
    s.databricks_host = "https://adb-1.17.azuredatabricks.net"
    s.databricks_token = token
    return s


def _domain(token=""):
    d = MagicMock()
    d.databricks = {"token": token} if token else {}
    return d


class TestClientUsesTheUserToken:
    def _build(self, *, user_token="", env_token="", domain_token="", implicit=False):
        with patch.object(DatabricksHelpers, "resolve_warehouse_id", return_value="w"), \
             patch.object(DatabricksHelpers, "resolve_use_cloud_fetch", return_value=False), \
             patch("back.core.helpers.DatabricksHelpers._databricks") as dbx:
            dbx.has_implicit_credentials.return_value = implicit
            DatabricksHelpers.get_databricks_client(
                _domain(domain_token), _settings(env_token), user_token=user_token
            )
            return dbx.DatabricksClient.call_args

    def test_user_token_is_passed_to_the_client(self, no_global_host):
        call = self._build(user_token=_USER_TOKEN)
        assert call is not None, "no client was built"
        assert call.kwargs["token"] == _USER_TOKEN

    def test_user_token_beats_the_environment_token(self, no_global_host):
        call = self._build(user_token=_USER_TOKEN, env_token=_ENV_TOKEN)
        assert call.kwargs["token"] == _USER_TOKEN

    def test_user_token_beats_implicit_service_principal(self, no_global_host):
        """Acting as the caller is more correct and more restrictive than
        acting as the app, so an explicit token must win."""
        call = self._build(user_token=_USER_TOKEN, implicit=True)
        assert call.kwargs["token"] == _USER_TOKEN

    def test_without_a_user_token_the_environment_is_used(self, no_global_host):
        call = self._build(env_token=_ENV_TOKEN)
        assert call.kwargs["token"] == _ENV_TOKEN

    def test_a_client_is_built_with_only_a_user_token(self, no_global_host):
        """The regression: no SP, no PAT, but a logged-in user. This returned
        None, which surfaced as "Databricks not configured"."""
        with patch.object(DatabricksHelpers, "resolve_warehouse_id", return_value="w"), \
             patch.object(DatabricksHelpers, "resolve_use_cloud_fetch", return_value=False), \
             patch("back.core.helpers.DatabricksHelpers._databricks") as dbx:
            dbx.has_implicit_credentials.return_value = False
            client = DatabricksHelpers.get_databricks_client(
                _domain(), _settings(), user_token=_USER_TOKEN
            )
        assert client is not None


class TestFetchWarehousesForwardsIt:
    @pytest.mark.asyncio
    async def test_the_token_reaches_the_client(self):
        from back.objects.domain.SettingsService import SettingsService

        with patch(
            "back.objects.domain.SettingsService.get_databricks_client"
        ) as gdc, patch("back.objects.domain.SettingsService.get_domain"), patch(
            "back.objects.domain.SettingsService.run_blocking",
            return_value=[],
        ):
            gdc.return_value = MagicMock()
            await SettingsService.fetch_warehouses(
                MagicMock(), MagicMock(), _USER_TOKEN
            )
        assert gdc.call_args.kwargs.get("user_token") == _USER_TOKEN


class TestRoutesSupplyIt:
    """Plumbing a parameter nothing populates would be inert."""

    _ROUTER = "src/api/routers/internal/settings.py"

    @pytest.mark.parametrize(
        "method",
        [
            "fetch_warehouses",
            "test_connection",
            "fetch_catalogs",
            "fetch_schemas",
            "fetch_volumes",
            "fetch_uc_assets",
            "fetch_uc_functions",
            "check_registry_access",
        ],
    )
    def test_every_interactive_route_passes_a_token(self, method):
        import re
        from pathlib import Path

        src = Path(self._ROUTER).read_text()
        for m in re.finditer(rf"config_service\.{method}\(", src):
            # scan the call's argument text up to a blank line
            tail = src[m.end() : m.end() + 400]
            call = tail[: tail.index("\n\n")] if "\n\n" in tail else tail
            assert "user_token" in call or "_settings_request_identity" in call, (
                f"{method} is called without forwarding the caller's token"
            )
