"""App-level access, backed by the registry rather than a Databricks App ACL.

``PermissionService`` used to derive app-level access from the Databricks App
ACL (``list_app_principals``): CAN_MANAGE meant admin, appearing in the ACL
meant app-user. Outside Databricks Apps that ACL does not exist, so the same
question is answered from an ``app_roles`` table in the registry.

Domain-level roles (viewer / editor / builder) are unaffected — they already
lived in ``domain_permissions``.

**Bootstrap.** A fresh deployment has an empty table, which would lock everyone
out including whoever is deploying. ``ONTOBRICKS_BOOTSTRAP_ADMIN`` names the
first admin and is seeded on first use. It is applied only when the table holds
no admin at all, so it cannot be used to re-grant access after an admin has
deliberately revoked it.
"""

from __future__ import annotations

import os
from typing import Any

from back.core.logging import get_logger

logger = get_logger(__name__)

# Re-exported so `from ...AppRoleService import ROLE_ADMIN` keeps working.
from back.objects.registry.roles import (  # noqa: E402,F401
    APP_LEVEL_ROLES as _VALID_ROLES,
    ROLE_ADMIN,
    ROLE_APP_USER,
    ROLE_NONE,
)


class AppRoleService:
    """Resolve and manage app-level access from the registry."""

    @staticmethod
    def bootstrap_admin() -> str:
        return (os.environ.get("ONTOBRICKS_BOOTSTRAP_ADMIN") or "").strip()

    @staticmethod
    def _match(principal: str, email: str, groups: list[str]) -> bool:
        p = (principal or "").strip().lower()
        if not p:
            return False
        if p == (email or "").strip().lower():
            return True
        return p in {str(g).strip().lower() for g in groups}

    @classmethod
    def ensure_bootstrap_admin(cls, store: Any) -> None:
        """Seed the bootstrap admin when no admin exists yet.

        Idempotent, and deliberately conditional on there being *no* admin: an
        operator who removes their own admin grant should not have it silently
        restored by a stale environment variable.
        """
        email = cls.bootstrap_admin()
        if not email:
            return
        try:
            rows = store.list_app_roles()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ensure_bootstrap_admin: could not list roles: %s", exc)
            return
        if any((r.get("role") or "") == ROLE_ADMIN for r in rows):
            return
        ok, msg = store.grant_app_role(
            email, ROLE_ADMIN, principal_type="user", display_name=email
        )
        logger.info("Bootstrap admin %s: %s", "seeded" if ok else "seed failed", msg)

    @classmethod
    def resolve_role(
        cls, store: Any, email: str, groups: list[str] | None = None
    ) -> str:
        """Return ``admin`` / ``app_user`` / ``none`` for *email*.

        A group grant matches when the caller's ``groups`` contain it, which is
        how the OIDC flow's SCIM group list feeds in. Admin wins over app-user
        when both match.
        """
        if not email:
            return ROLE_NONE
        groups = groups or []
        cls.ensure_bootstrap_admin(store)
        try:
            rows = store.list_app_roles()
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolve_role: could not list app roles: %s", exc)
            return ROLE_NONE

        best = ROLE_NONE
        for row in rows:
            if not cls._match(row.get("principal", ""), email, groups):
                continue
            role = (row.get("role") or "").strip().lower()
            if role == ROLE_ADMIN:
                return ROLE_ADMIN
            if role == ROLE_APP_USER:
                best = ROLE_APP_USER
        return best

    @classmethod
    def is_admin(cls, store: Any, email: str, groups: list[str] | None = None) -> bool:
        return cls.resolve_role(store, email, groups) == ROLE_ADMIN

    @classmethod
    def list_roles(cls, store: Any) -> list[dict[str, Any]]:
        cls.ensure_bootstrap_admin(store)
        return store.list_app_roles()

    @classmethod
    def grant(
        cls,
        store: Any,
        principal: str,
        role: str,
        *,
        principal_type: str = "user",
        display_name: str = "",
    ) -> tuple[bool, str]:
        role = (role or "").strip().lower()
        if role not in _VALID_ROLES:
            return False, f"role must be one of {', '.join(_VALID_ROLES)}"
        if principal_type not in ("user", "group"):
            return False, "principal_type must be 'user' or 'group'"
        return store.grant_app_role(
            principal, role, principal_type=principal_type, display_name=display_name
        )

    @classmethod
    def revoke(
        cls, store: Any, principal: str, *, actor_email: str = ""
    ) -> tuple[bool, str]:
        """Remove a grant, refusing to strip the last admin.

        Without this an admin could revoke themselves — or the only other
        admin — and leave the deployment unadministerable, recoverable only by
        editing the database directly.
        """
        rows = store.list_app_roles()
        target = (principal or "").strip().lower()
        admins = [r for r in rows if (r.get("role") or "") == ROLE_ADMIN]
        if len(admins) <= 1 and any(
            (r.get("principal") or "").strip().lower() == target for r in admins
        ):
            return False, (
                "Refusing to revoke the last admin — grant admin to another "
                "principal first."
            )
        _ = actor_email
        return store.revoke_app_role(principal)
