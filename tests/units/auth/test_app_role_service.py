"""Tests for AppRoleService — app-level access from the registry.

Replaces the Databricks App ACL (`list_app_principals`), which does not exist
outside Databricks Apps. The two behaviours worth pinning are the bootstrap
(a fresh deployment must not lock everyone out) and the last-admin guard
(an admin must not be able to make the deployment unadministerable).
"""

from __future__ import annotations

import pytest

from back.objects.registry.AppRoleService import (
    ROLE_ADMIN,
    ROLE_APP_USER,
    ROLE_NONE,
    AppRoleService,
)

pytestmark = pytest.mark.unit


class _FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.granted = []
        self.revoked = []

    def list_app_roles(self):
        return list(self.rows)

    def grant_app_role(
        self, principal, role, *, principal_type="user", display_name=""
    ):
        self.granted.append((principal, role, principal_type))
        self.rows = [
            r for r in self.rows if r["principal"].lower() != principal.lower()
        ]
        self.rows.append(
            {
                "principal": principal,
                "role": role,
                "principal_type": principal_type,
                "display_name": display_name,
            }
        )
        return True, f"granted {role} to {principal}"

    def revoke_app_role(self, principal):
        self.revoked.append(principal)
        self.rows = [
            r for r in self.rows if r["principal"].lower() != principal.lower()
        ]
        return True, f"revoked {principal}"


def _row(principal, role, ptype="user"):
    return {
        "principal": principal,
        "role": role,
        "principal_type": ptype,
        "display_name": "",
    }


@pytest.fixture(autouse=True)
def _no_bootstrap(monkeypatch):
    monkeypatch.delenv("ONTOBRICKS_BOOTSTRAP_ADMIN", raising=False)


class TestResolveRole:
    def test_direct_admin_grant(self):
        store = _FakeStore([_row("ada@example.com", "admin")])
        assert AppRoleService.resolve_role(store, "ada@example.com") == ROLE_ADMIN

    def test_match_is_case_insensitive(self):
        store = _FakeStore([_row("Ada@Example.com", "admin")])
        assert AppRoleService.resolve_role(store, "ada@example.com") == ROLE_ADMIN

    def test_group_grant_matches_via_groups(self):
        store = _FakeStore([_row("data-eng", "app_user", "group")])
        role = AppRoleService.resolve_role(store, "ada@example.com", ["data-eng"])
        assert role == ROLE_APP_USER

    def test_group_grant_ignored_without_membership(self):
        store = _FakeStore([_row("data-eng", "app_user", "group")])
        assert AppRoleService.resolve_role(store, "ada@example.com", []) == ROLE_NONE

    def test_admin_wins_over_app_user(self):
        store = _FakeStore(
            [
                _row("eng", "app_user", "group"),
                _row("ada@example.com", "admin"),
            ]
        )
        assert (
            AppRoleService.resolve_role(store, "ada@example.com", ["eng"]) == ROLE_ADMIN
        )

    def test_no_match_is_none(self):
        store = _FakeStore([_row("bob@example.com", "admin")])
        assert AppRoleService.resolve_role(store, "ada@example.com") == ROLE_NONE

    def test_empty_email_is_none(self):
        assert AppRoleService.resolve_role(_FakeStore(), "") == ROLE_NONE

    def test_store_failure_degrades_to_none(self):
        class _Broken:
            def list_app_roles(self):
                raise RuntimeError("registry unreachable")

        assert AppRoleService.resolve_role(_Broken(), "ada@example.com") == ROLE_NONE


class TestBootstrap:
    def test_seeds_the_first_admin(self, monkeypatch):
        """A fresh deployment must not lock everyone out."""
        monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", "ada@example.com")
        store = _FakeStore([])
        assert AppRoleService.resolve_role(store, "ada@example.com") == ROLE_ADMIN
        assert store.granted == [("ada@example.com", "admin", "user")]

    def test_not_applied_when_an_admin_already_exists(self, monkeypatch):
        """A revoked admin must not be silently restored by a stale env var."""
        monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", "ada@example.com")
        store = _FakeStore([_row("bob@example.com", "admin")])
        assert AppRoleService.resolve_role(store, "ada@example.com") == ROLE_NONE
        assert store.granted == []

    def test_is_idempotent(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", "ada@example.com")
        store = _FakeStore([])
        AppRoleService.ensure_bootstrap_admin(store)
        AppRoleService.ensure_bootstrap_admin(store)
        assert len(store.granted) == 1

    def test_unset_is_a_no_op(self):
        store = _FakeStore([])
        AppRoleService.ensure_bootstrap_admin(store)
        assert store.granted == []


class TestGrant:
    def test_rejects_an_unknown_role(self):
        ok, msg = AppRoleService.grant(_FakeStore(), "ada@example.com", "wizard")
        assert ok is False and "role must be one of" in msg

    def test_rejects_an_unknown_principal_type(self):
        ok, msg = AppRoleService.grant(
            _FakeStore(), "ada@example.com", "admin", principal_type="robot"
        )
        assert ok is False and "principal_type" in msg

    def test_grants_a_valid_role(self):
        store = _FakeStore()
        ok, _ = AppRoleService.grant(store, "ada@example.com", "app_user")
        assert ok is True
        assert store.granted == [("ada@example.com", "app_user", "user")]


class TestRevoke:
    def test_refuses_to_remove_the_last_admin(self):
        """Otherwise the deployment becomes unadministerable.

        Recovery would mean editing the database by hand.
        """
        store = _FakeStore([_row("ada@example.com", "admin")])
        ok, msg = AppRoleService.revoke(store, "ada@example.com")
        assert ok is False
        assert "last admin" in msg
        assert store.revoked == []

    def test_allows_removing_an_admin_when_another_remains(self):
        store = _FakeStore(
            [
                _row("ada@example.com", "admin"),
                _row("bob@example.com", "admin"),
            ]
        )
        ok, _ = AppRoleService.revoke(store, "ada@example.com")
        assert ok is True
        assert store.revoked == ["ada@example.com"]

    def test_allows_removing_a_non_admin(self):
        store = _FakeStore(
            [
                _row("ada@example.com", "admin"),
                _row("bob@example.com", "app_user"),
            ]
        )
        ok, _ = AppRoleService.revoke(store, "bob@example.com")
        assert ok is True
