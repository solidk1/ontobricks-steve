"""Tests for the OIDC login flow.

The security-relevant parts are PKCE (an intercepted authorization code is
useless without the verifier), the `state` check (a forged callback must be
rejected), and the redirect allow-list (no open redirect).
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from back.objects.identity.OIDCClient import DEFAULT_SCOPES, OIDCClient

pytestmark = pytest.mark.unit


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
    monkeypatch.setenv("ONTOBRICKS_OIDC_CLIENT_ID", "client-abc")
    monkeypatch.setenv(
        "ONTOBRICKS_OIDC_REDIRECT_URI", "https://app.example/auth/callback"
    )
    monkeypatch.delenv("ONTOBRICKS_OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ONTOBRICKS_OIDC_SCOPES", raising=False)


class TestConfiguration:
    def test_unconfigured_reports_what_is_missing(self, monkeypatch):
        for var in (
            "DATABRICKS_HOST",
            "ONTOBRICKS_OIDC_CLIENT_ID",
            "ONTOBRICKS_OIDC_REDIRECT_URI",
        ):
            monkeypatch.delenv(var, raising=False)
        client = OIDCClient()
        assert client.is_configured is False
        assert set(client.missing_config()) == {
            "DATABRICKS_HOST",
            "ONTOBRICKS_OIDC_CLIENT_ID",
            "ONTOBRICKS_OIDC_REDIRECT_URI",
        }

    def test_configured_when_all_present(self, oidc_env):
        assert OIDCClient().is_configured is True

    def test_endpoints_derive_from_the_host(self, oidc_env):
        c = OIDCClient()
        assert (
            c.authorize_endpoint == "https://ws.cloud.databricks.com/oidc/v1/authorize"
        )
        assert c.token_endpoint == "https://ws.cloud.databricks.com/oidc/v1/token"

    def test_bare_host_gets_https(self, monkeypatch, oidc_env):
        monkeypatch.setenv("DATABRICKS_HOST", "ws.cloud.databricks.com")
        assert OIDCClient().host == "https://ws.cloud.databricks.com"

    def test_default_scopes_request_offline_access(self, oidc_env):
        """offline_access is what lets a session outlive one access token."""
        assert "offline_access" in OIDCClient().scopes
        assert "all-apis" in DEFAULT_SCOPES


class TestPKCE:
    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = OIDCClient._pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == expected

    def test_pairs_are_unique(self):
        assert OIDCClient._pkce_pair()[0] != OIDCClient._pkce_pair()[0]

    def test_no_base64_padding(self):
        """Padding is not allowed in the PKCE parameters."""
        verifier, challenge = OIDCClient._pkce_pair()
        assert "=" not in verifier and "=" not in challenge


class TestBegin:
    def test_authorize_url_carries_the_required_parameters(self, oidc_env):
        url, state, verifier = OIDCClient().begin()
        assert url.startswith("https://ws.cloud.databricks.com/oidc/v1/authorize?")
        for fragment in (
            "client_id=client-abc",
            "response_type=code",
            "code_challenge_method=S256",
            f"state={state}",
        ):
            assert fragment in url
        assert (
            verifier and verifier not in url
        ), "the verifier must never leave the server"

    def test_unconfigured_begin_raises(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_OIDC_CLIENT_ID", raising=False)
        from back.core.errors import ValidationError

        with pytest.raises(ValidationError, match="not configured"):
            OIDCClient().begin()


class TestReturnToSafety:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://evil.example/steal",
            "//evil.example",
            "http://evil.example",
        ],
    )
    def test_absolute_urls_are_rejected(self, raw):
        """Otherwise ?return_to= is an open redirect off the login route."""
        from api.routers.internal.auth import _safe_return_to

        assert _safe_return_to(raw) == "/"

    @pytest.mark.parametrize("raw", ["/", "/settings", "/domain/abc?tab=1"])
    def test_relative_paths_are_kept(self, raw):
        from api.routers.internal.auth import _safe_return_to

        assert _safe_return_to(raw) == raw

    def test_empty_falls_back_to_root(self):
        from api.routers.internal.auth import _safe_return_to

        assert _safe_return_to("") == "/"
