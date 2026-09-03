"""The fail-closed guarantee.

``auth_enabled()`` defaults to on, so a deployment that configures nothing is
locked rather than serving every domain unauthenticated. This is the test that
would have caught the pre-P1 conflation, where a non-Apps deployment silently
got neither TLS-only cookies nor access control.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestDefaults:
    def test_auth_is_on_when_nothing_is_configured(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        for var in ("ONTOBRICKS_AUTH_ENABLED", "DATABRICKS_APP_PORT"):
            monkeypatch.delenv(var, raising=False)
        assert RuntimeEnv.auth_enabled() is True

    def test_secure_cookies_are_independent_of_auth(self, monkeypatch):
        """Conflating these is what left non-Apps deployments with neither."""
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_AUTH_ENABLED", raising=False)
        monkeypatch.delenv("ONTOBRICKS_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.auth_enabled() is True
        assert RuntimeEnv.secure_cookies() is False


class TestAnonymousIsNotAdmin:
    def test_local_scim_fallback_is_suppressed_under_enforcement(self, monkeypatch):
        """The dangerous case.

        With auth enforced, resolving an anonymous request via SCIM /Me would
        authenticate it as the deploying principal — silently granting whatever
        that principal has.
        """
        from back.objects.identity import Identity, IdentityResolver

        monkeypatch.setattr(
            IdentityResolver,
            "from_local_workspace",
            staticmethod(lambda: Identity(email="deployer@example.com")),
        )

        class _Req:
            headers: dict = {}
            state = None

        anonymous = IdentityResolver.resolve(_Req(), allow_local=False)
        assert anonymous.is_authenticated is False
        assert anonymous.email == ""


class TestLoginRouteIsReachableWithoutAuth:
    def test_auth_prefix_bypasses_the_permission_middleware(self):
        """You cannot require a login in order to log in."""
        from shared.fastapi.main import _PERM_BYPASS_PREFIXES

        assert "/auth/" in _PERM_BYPASS_PREFIXES

    def test_login_reports_missing_configuration(self, monkeypatch):
        """An unconfigured deployment must say what is missing, not 500."""
        from fastapi.testclient import TestClient

        for var in ("ONTOBRICKS_OIDC_CLIENT_ID", "ONTOBRICKS_OIDC_REDIRECT_URI"):
            monkeypatch.delenv(var, raising=False)
        from shared.fastapi.main import create_app

        client = TestClient(create_app())
        resp = client.get("/auth/login", follow_redirects=False)
        assert resp.status_code == 503
        assert "ONTOBRICKS_OIDC_CLIENT_ID" in resp.text


class TestCallbackRejectsForgedState:
    def test_missing_state_is_rejected(self):
        from fastapi.testclient import TestClient

        from shared.fastapi.main import create_app

        client = TestClient(create_app())
        resp = client.get("/auth/callback?code=abc", follow_redirects=False)
        assert resp.status_code == 400
        assert "login state" in resp.text.lower()

    def test_mismatched_state_is_rejected(self):
        """Accepting an unmatched state would allow a forged callback."""
        from fastapi.testclient import TestClient

        from shared.fastapi.main import create_app

        client = TestClient(create_app())
        resp = client.get(
            "/auth/callback?code=abc&state=not-the-one", follow_redirects=False
        )
        assert resp.status_code == 400
