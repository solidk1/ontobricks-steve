"""The bootstrap admin must work before the registry exists.

A fresh deployment has no ``app_roles`` table until the registry is initialised,
and initialising it happens in the UI behind an admin check. That deadlocked:
``resolve_role`` could not read the table, so everyone resolved to ``none``, so
nobody could reach Settings -> Registry -> Initialize to create the table that
would have given them a role. ``ONTOBRICKS_BOOTSTRAP_ADMIN`` exists to break
exactly that, and only worked *after* the thing it was meant to unblock.

Observed on a live Azure deployment: sign-in succeeded, and the app still served
Access Denied to the configured bootstrap address.
"""

import pytest

from back.objects.registry.AppRoleService import AppRoleService

pytestmark = pytest.mark.unit

_BOOT = "boot.admin@example.com"


class _FreshStore:
    """A registry whose tables do not exist yet."""

    def list_app_roles(self):
        raise RuntimeError('relation "ontobricks_registry.app_roles" does not exist')

    def grant_app_role(self, *a, **k):
        raise RuntimeError('relation "ontobricks_registry.app_roles" does not exist')


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", _BOOT)
    return _FreshStore()


class TestDeadlockIsBroken:
    def test_bootstrap_admin_resolves_to_admin(self, fresh):
        assert AppRoleService.resolve_role(fresh, _BOOT) == "admin"

    def test_is_admin_agrees(self, fresh):
        assert AppRoleService.is_admin(fresh, _BOOT) is True

    def test_match_is_case_insensitive(self, fresh):
        assert AppRoleService.resolve_role(fresh, _BOOT.upper()) == "admin"


class TestItGrantsNobodyElse:
    def test_a_different_user_still_gets_none(self, fresh):
        assert AppRoleService.resolve_role(fresh, "someone.else@example.com") == "none"

    def test_empty_email_gets_none(self, fresh):
        assert AppRoleService.resolve_role(fresh, "") == "none"

    def test_a_group_named_like_the_admin_does_not_match_by_accident(self, fresh):
        """Group membership must not be a path to the bootstrap grant."""
        assert (
            AppRoleService.resolve_role(fresh, "other@example.com", groups=["admins"])
            == "none"
        )

    def test_without_the_variable_set_nobody_gets_in(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_BOOTSTRAP_ADMIN", raising=False)
        assert AppRoleService.resolve_role(_FreshStore(), _BOOT) == "none"


class TestNormalOperationUnchanged:
    def test_a_readable_table_still_decides(self, monkeypatch):
        """With the table present, grants come from it -- not from the env var."""
        monkeypatch.setenv("ONTOBRICKS_BOOTSTRAP_ADMIN", _BOOT)

        class Store:
            def list_app_roles(self):
                return [{"principal": "someone@example.com", "role": "admin"}]

            def grant_app_role(self, *a, **k):
                return True, "ok"

        store = Store()
        assert AppRoleService.resolve_role(store, "someone@example.com") == "admin"
        # The bootstrap address is not in the table and an admin already exists,
        # so ensure_bootstrap_admin must not seed it.
        assert AppRoleService.resolve_role(store, _BOOT) == "none"
