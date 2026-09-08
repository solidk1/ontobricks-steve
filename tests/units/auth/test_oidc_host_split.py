"""The OIDC issuer host and the workspace API host are separately configurable.

Why this file exists
--------------------
``OIDCClient`` derived every endpoint from ``DATABRICKS_HOST``. On Azure those
are routinely two different hosts:

* a custom app integration registered in the **account** console is issued by
  the account host, and some deployments front that with a vanity domain;
* Unity Catalog, SQL warehouses and SCIM live on the **workspace** host
  ``adb-<id>.<n>.azuredatabricks.net``.

A vanity/account host answers OIDC discovery correctly and returns **HTTP 303**
for every ``/api/2.0/...`` path. So pointing ``DATABRICKS_HOST`` at it made
login work and broke both SCIM ``/Me`` and warehouse listing; pointing it at the
workspace would have done the reverse. One variable could not be both, and the
symptom showed up as three apparently unrelated failures.
"""

from __future__ import annotations

import pytest

from back.objects.identity.OIDCClient import OIDCClient

pytestmark = pytest.mark.unit

_ACCOUNT = "https://oneenv.azuredatabricks.net"
_WORKSPACE = "https://adb-7405611364794897.17.azuredatabricks.net"


@pytest.fixture
def split_hosts(monkeypatch):
    monkeypatch.setenv("ONTOBRICKS_OIDC_HOST", _ACCOUNT)
    monkeypatch.setenv("DATABRICKS_HOST", _WORKSPACE)
    monkeypatch.setenv("ONTOBRICKS_OIDC_CLIENT_ID", "cid")
    monkeypatch.setenv("ONTOBRICKS_OIDC_REDIRECT_URI", "https://app.example/cb")
    yield


class TestSplitHosts:
    def test_authorize_uses_the_issuer(self, split_hosts):
        assert OIDCClient().authorize_endpoint == f"{_ACCOUNT}/oidc/v1/authorize"

    def test_token_uses_the_issuer(self, split_hosts):
        assert OIDCClient().token_endpoint == f"{_ACCOUNT}/oidc/v1/token"

    def test_scim_uses_the_workspace(self, split_hosts):
        """SCIM is a workspace API. Sent to an account host it 303s, which is
        how "Could not resolve the signed-in user via SCIM /Me" happened."""
        assert OIDCClient().workspace_host == _WORKSPACE

    def test_the_two_hosts_are_not_the_same(self, split_hosts):
        c = OIDCClient()
        assert c.host != c.workspace_host


class TestSingleHostStillWorks:
    """The common case is one host for both; it must need no new variable."""

    @pytest.fixture
    def one_host(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_OIDC_HOST", raising=False)
        monkeypatch.setenv("DATABRICKS_HOST", _WORKSPACE)
        monkeypatch.setenv("ONTOBRICKS_OIDC_CLIENT_ID", "cid")
        monkeypatch.setenv("ONTOBRICKS_OIDC_REDIRECT_URI", "https://app.example/cb")
        yield

    def test_issuer_falls_back_to_databricks_host(self, one_host):
        assert OIDCClient().host == _WORKSPACE

    def test_workspace_matches_the_issuer(self, one_host):
        c = OIDCClient()
        assert c.workspace_host == c.host == _WORKSPACE

    def test_still_configured(self, one_host):
        assert OIDCClient().is_configured is True


class TestSchemeIsAdded:
    @pytest.mark.parametrize("field", ["host", "workspace_host"])
    def test_bare_hostname_gets_https(self, monkeypatch, field):
        monkeypatch.setenv("ONTOBRICKS_OIDC_HOST", "oneenv.azuredatabricks.net")
        monkeypatch.setenv("DATABRICKS_HOST", "adb-1.17.azuredatabricks.net")
        assert getattr(OIDCClient(), field).startswith("https://")
