"""The registry's role vocabulary — one definition, two consumers.

``PermissionService`` and ``AppRoleService`` both need these strings:
``AppRoleService.resolve_role`` returns them and ``PermissionService`` compares
against them. They were declared separately in each module, so a typo or a rename
in one would have silently broken every permission check that crossed the
boundary — the values are compared by equality, never validated against a shared
list.

They cannot simply import from each other: ``PermissionService`` already imports
``AppRoleService`` (lazily, inside ``_app_role_from_registry``), so a module-level
import back would close a cycle. Hence a leaf module that imports nothing.
"""

from typing import Dict

#: Full app + domain role vocabulary.
ROLE_ADMIN = "admin"
ROLE_BUILDER = "builder"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLE_APP_USER = "app_user"
ROLE_NONE = "none"

#: Ordering for "at least this role" comparisons.
ROLE_HIERARCHY: Dict[str, int] = {
    ROLE_NONE: 0,
    ROLE_VIEWER: 1,
    ROLE_EDITOR: 2,
    ROLE_BUILDER: 3,
    ROLE_ADMIN: 4,
}

#: Roles an admin may hand out per domain.
ASSIGNABLE_ROLES = (ROLE_VIEWER, ROLE_EDITOR, ROLE_BUILDER)

#: Roles storable in the app-level ``app_roles`` table.
APP_LEVEL_ROLES = (ROLE_ADMIN, ROLE_APP_USER)

__all__ = [
    "ROLE_ADMIN",
    "ROLE_BUILDER",
    "ROLE_EDITOR",
    "ROLE_VIEWER",
    "ROLE_APP_USER",
    "ROLE_NONE",
    "ROLE_HIERARCHY",
    "ASSIGNABLE_ROLES",
    "APP_LEVEL_ROLES",
]
