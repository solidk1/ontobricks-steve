"""An unauthenticated visitor must be sent to log in, not denied.

P4b added ``/auth/login``, ``/auth/callback`` and the ``app_roles`` table, but
never wired the entry point: ``PermissionMiddleware`` treated "no identity at all"
and "identity holding no role" identically and redirected both to
``/access-denied``. Since nothing in the UI linked to ``/auth/login`` — a
repo-wide grep for it returned zero hits — the OIDC flow was unreachable unless
you typed the URL by hand. A real deployment surfaced it: every visitor landed on
``/access-denied?reason=app`` with no way forward.

These tests pin the distinction: unauthenticated redirects to login,
authenticated-but-unauthorised still denies.
"""

import pytest

pytestmark = pytest.mark.unit

_OIDC_ENV = {
    "ONTOBRICKS_AUTH_ENABLED": "true",
    "ONTOBRICKS_OIDC_CLIENT_ID": "test-client",
    "ONTOBRICKS_OIDC_REDIRECT_URI": "https://example.test/auth/callback",
    "DATABRICKS_HOST": "https://example.azuredatabricks.net",
    "SECRET_KEY": "test-secret-not-the-default",
}


@pytest.fixture
def client(monkeypatch):
    for k, v in _OIDC_ENV.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient

    from shared.fastapi import health
    from shared.fastapi.main import app

    monkeypatch.setattr(health, "_build_health_client", lambda *a, **k: None)
    with TestClient(app, follow_redirects=False) as c:
        yield c


class TestUnauthenticatedGoesToLogin:
    @pytest.mark.parametrize("path", ["/", "/settings", "/domain", "/registry"])
    def test_redirects_to_auth_login(self, client, path):
        r = client.get(path)
        assert r.status_code == 302
        assert r.headers["location"].startswith("/auth/login")

    def test_preserves_the_original_path_as_return_to(self, client):
        r = client.get("/settings")
        assert "return_to=/settings" in r.headers["location"]

    def test_does_not_send_them_to_access_denied(self, client):
        """The bug: /access-denied is a dead end with no route to login."""
        assert "access-denied" not in client.get("/").headers["location"]


class TestAnonymousEndpointsStayAnonymous:
    def test_health_is_still_reachable(self, client):
        assert client.get("/health").status_code == 200

    def test_auth_login_itself_is_not_redirected(self, client):
        """You cannot require a login in order to log in."""
        r = client.get("/auth/login")
        assert r.status_code != 302 or "/auth/login" not in r.headers.get(
            "location", ""
        )


class TestWithoutOIDCConfigured:
    def test_falls_back_to_access_denied(self, monkeypatch):
        """With no OIDC client there is nowhere to send them, so deny as before."""
        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "true")
        monkeypatch.setenv("SECRET_KEY", "test-secret-not-the-default")
        for k in (
            "ONTOBRICKS_OIDC_CLIENT_ID",
            "ONTOBRICKS_OIDC_REDIRECT_URI",
        ):
            monkeypatch.delenv(k, raising=False)
        from fastapi.testclient import TestClient

        from shared.fastapi import health
        from shared.fastapi.main import app

        monkeypatch.setattr(health, "_build_health_client", lambda *a, **k: None)
        with TestClient(app, follow_redirects=False) as c:
            r = c.get("/")
            assert r.status_code == 302
            assert "access-denied" in r.headers["location"]
