"""Tests for the app-role management endpoints.

Without these routes an admin could only grant access by running SQL against
Postgres: `AppRoleService` existed but nothing reachable called it. That made
RBAC unusable in practice, since `ONTOBRICKS_BOOTSTRAP_ADMIN` lets the first
admin in and then strands them.

The two behaviours worth pinning are that management is admin-only (this is the
one surface that can lock everybody out) and that the last admin cannot be
revoked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.errors import AuthorizationError, ValidationError

pytestmark = pytest.mark.unit

_MOD = "api.routers.internal.settings"


class _FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.granted = []
        self.revoked = []

    def list_app_roles(self):
        return list(self.rows)

    def grant_app_role(self, principal, role, *, principal_type="user", display_name=""):
        self.granted.append((principal, role, principal_type))
        return True, f"granted {role} to {principal}"

    def revoke_app_role(self, principal):
        self.revoked.append(principal)
        return True, f"revoked {principal}"


def _row(principal, role, ptype="user"):
    return {"principal": principal, "role": role, "principal_type": ptype,
            "display_name": ""}


def _request(role="admin", body=None):
    req = MagicMock()
    req.state.user_role = role
    req.state.user_email = "admin@example.com"

    async def _json():
        return body or {}

    req.json = _json
    return req


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    """These routes are only meaningful with enforcement on."""
    monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "true")
    monkeypatch.delenv("ONTOBRICKS_BOOTSTRAP_ADMIN", raising=False)


class TestAdminGuard:
    @pytest.mark.parametrize("role", ["app_user", "viewer", "editor", "builder", "none", ""])
    async def test_non_admin_is_rejected(self, role):
        import api.routers.internal.settings as mod

        for coro, body in (
            (mod.get_app_roles, None),
            (mod.post_app_role_grant, {"principal": "x@y.z", "role": "admin"}),
            (mod.post_app_role_revoke, {"principal": "x@y.z"}),
        ):
            with pytest.raises(AuthorizationError, match="admin"):
                await coro(_request(role=role, body=body), MagicMock(), MagicMock())

    async def test_guard_is_inert_when_auth_is_disabled(self, monkeypatch):
        """Local development has no identity to check."""
        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "false")
        import api.routers.internal.settings as mod

        store = _FakeStore([_row("a@b.c", "admin")])
        with patch.object(mod, "_app_role_store", return_value=store):
            out = await mod.get_app_roles(_request(role=""), MagicMock(), MagicMock())
        assert out["success"] is True


class TestList:
    async def test_returns_roles_and_bootstrap_admin(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", "seed@example.com")
        import api.routers.internal.settings as mod

        store = _FakeStore([_row("a@b.c", "admin")])
        with patch.object(mod, "_app_role_store", return_value=store):
            out = await mod.get_app_roles(_request(), MagicMock(), MagicMock())
        assert out["roles"] == [_row("a@b.c", "admin")]
        assert out["bootstrap_admin"] == "seed@example.com"


class TestGrant:
    async def test_grants_a_user(self):
        import api.routers.internal.settings as mod

        store = _FakeStore()
        with patch.object(mod, "_app_role_store", return_value=store):
            out = await mod.post_app_role_grant(
                _request(body={"principal": "new@example.com", "role": "app_user"}),
                MagicMock(), MagicMock(),
            )
        assert out["success"] is True
        assert store.granted == [("new@example.com", "app_user", "user")]

    async def test_grants_a_group(self):
        import api.routers.internal.settings as mod

        store = _FakeStore()
        with patch.object(mod, "_app_role_store", return_value=store):
            await mod.post_app_role_grant(
                _request(body={
                    "principal": "data-eng", "role": "app_user",
                    "principal_type": "group",
                }),
                MagicMock(), MagicMock(),
            )
        assert store.granted == [("data-eng", "app_user", "group")]

    async def test_missing_principal_is_rejected(self):
        import api.routers.internal.settings as mod

        with pytest.raises(ValidationError, match="principal"):
            await mod.post_app_role_grant(
                _request(body={"role": "admin"}), MagicMock(), MagicMock()
            )

    async def test_unknown_role_is_rejected(self):
        import api.routers.internal.settings as mod

        store = _FakeStore()
        with patch.object(mod, "_app_role_store", return_value=store):
            with pytest.raises(ValidationError, match="role must be one of"):
                await mod.post_app_role_grant(
                    _request(body={"principal": "x@y.z", "role": "wizard"}),
                    MagicMock(), MagicMock(),
                )
        assert store.granted == []


class TestRevoke:
    async def test_revokes_a_non_admin(self):
        import api.routers.internal.settings as mod

        store = _FakeStore([_row("a@b.c", "admin"), _row("b@b.c", "app_user")])
        with patch.object(mod, "_app_role_store", return_value=store):
            out = await mod.post_app_role_revoke(
                _request(body={"principal": "b@b.c"}), MagicMock(), MagicMock()
            )
        assert out["success"] is True
        assert store.revoked == ["b@b.c"]

    async def test_last_admin_cannot_be_revoked(self):
        """Otherwise the deployment becomes unadministerable."""
        import api.routers.internal.settings as mod

        store = _FakeStore([_row("only@example.com", "admin")])
        with patch.object(mod, "_app_role_store", return_value=store):
            with pytest.raises(ValidationError, match="last admin"):
                await mod.post_app_role_revoke(
                    _request(body={"principal": "only@example.com"}),
                    MagicMock(), MagicMock(),
                )
        assert store.revoked == []

    async def test_admin_revocable_when_another_remains(self):
        import api.routers.internal.settings as mod

        store = _FakeStore([_row("a@b.c", "admin"), _row("b@b.c", "admin")])
        with patch.object(mod, "_app_role_store", return_value=store):
            await mod.post_app_role_revoke(
                _request(body={"principal": "a@b.c"}), MagicMock(), MagicMock()
            )
        assert store.revoked == ["a@b.c"]

    async def test_missing_principal_is_rejected(self):
        import api.routers.internal.settings as mod

        with pytest.raises(ValidationError, match="principal"):
            await mod.post_app_role_revoke(
                _request(body={}), MagicMock(), MagicMock()
            )
