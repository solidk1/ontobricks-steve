"""An OntoBricks admin can administer OntoBricks.

Why this file exists
--------------------
``PermissionService.is_admin`` only ever read ``CAN_MANAGE`` on the *Databricks
App* ACL (``GET /api/2.0/permissions/apps/<name>``). Off the Apps platform there
is no app to hold an ACL, so every path returned "could not determine",
``bool(None)`` became ``False``, and **nobody could be an admin for writes** no
matter what the registry ``app_roles`` table said.

The visible result: the Settings page showed the user's own **Admin** badge —
read from ``app_roles`` — directly above a panel that refused every save with
"Only admins (CAN MANAGE) can change the SQL Warehouse". Two implementations of
one question, disagreeing, exactly like the ``auth_mode`` badge.

``app_roles`` is now the authority and the App ACL is a fallback for deployments
still on the Apps platform.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.objects.registry.PermissionService import PermissionService
from back.objects.registry.roles import ROLE_ADMIN, ROLE_APP_USER, ROLE_NONE

pytestmark = pytest.mark.unit

_CFG = {"postgres_schema": "obreg"}
_EMAIL = "admin@example.com"


def _svc():
    s = PermissionService()
    s._admin_cache = {}
    return s


class TestRegistryRoleIsAuthoritative:
    def test_app_roles_admin_is_admin_without_any_databricks_app(self):
        svc = _svc()
        with patch.object(
            PermissionService, "_app_role_from_registry", return_value=ROLE_ADMIN
        ), patch.object(PermissionService, "_check_admin_sdk") as sdk:
            assert svc.is_admin(_EMAIL, "", "", "", registry_cfg=_CFG) is True
        sdk.assert_not_called(), "the ACL must not even be consulted"

    def test_admin_works_with_no_app_name(self):
        """Off the Apps platform ``ontobricks_app_name`` is typically empty, and
        that used to short-circuit to False before any role lookup."""
        svc = _svc()
        with patch.object(
            PermissionService, "_app_role_from_registry", return_value=ROLE_ADMIN
        ):
            assert svc.is_admin(_EMAIL, "h", "t", "", registry_cfg=_CFG) is True

    @pytest.mark.parametrize("role", [ROLE_APP_USER, ROLE_NONE])
    def test_non_admin_registry_roles_are_not_admin(self, role):
        svc = _svc()
        with patch.object(
            PermissionService, "_app_role_from_registry", return_value=role
        ):
            assert svc.is_admin(_EMAIL, "", "", "", registry_cfg=_CFG) is False

    def test_no_email_is_never_admin(self):
        assert _svc().is_admin("", "h", "t", "app", registry_cfg=_CFG) is False


class TestAppACLRemainsAFallback:
    """Deployments still on the Apps platform must keep working."""

    def test_acl_grants_admin_when_the_registry_has_no_grant(self):
        svc = _svc()
        with patch.object(
            PermissionService, "_app_role_from_registry", return_value=ROLE_NONE
        ), patch.object(PermissionService, "_check_admin_sdk", return_value=True):
            assert svc.is_admin(_EMAIL, "h", "t", "myapp", registry_cfg=_CFG) is True

    def test_acl_denial_is_a_denial(self):
        svc = _svc()
        with patch.object(
            PermissionService, "_app_role_from_registry", return_value=ROLE_NONE
        ), patch.object(PermissionService, "_check_admin_sdk", return_value=False):
            assert svc.is_admin(_EMAIL, "h", "t", "myapp", registry_cfg=_CFG) is False

    def test_works_without_registry_cfg_at_all(self):
        """Legacy callers pass no registry_cfg; they must still reach the ACL."""
        svc = _svc()
        with patch.object(PermissionService, "_check_admin_sdk", return_value=True):
            assert svc.is_admin(_EMAIL, "h", "t", "myapp") is True


class TestErrorMessage:
    def test_denial_does_not_mention_databricks_app_permissions(self):
        """"Only admins (CAN MANAGE) can change the SQL Warehouse" was wrong
        twice: CAN MANAGE is an Apps ACL level that no longer applies, and the
        message named the SQL Warehouse whatever setting was being changed."""
        from back.core.errors import AuthorizationError
        from back.objects.domain.SettingsService import SettingsService

        with patch("shared.config.RuntimeEnv.RuntimeEnv.auth_enabled", return_value=True), \
             patch.object(
                 SettingsService, "_resolve_context",
                 return_value=(MagicMock(), "h", "t", _CFG),
             ), \
             patch(
                 "back.objects.domain.SettingsService.permission_service.is_admin",
                 return_value=False,
             ):
            with pytest.raises(AuthorizationError) as exc:
                SettingsService.require_admin_error(
                    _EMAIL, "utok", MagicMock(), MagicMock()
                )
        msg = str(exc.value)
        assert "CAN MANAGE" not in msg
        assert "SQL Warehouse" not in msg
        assert "admin" in msg.lower()
