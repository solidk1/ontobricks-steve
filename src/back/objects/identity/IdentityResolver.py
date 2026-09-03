"""Single source of truth for "who is making this request".

Identity used to be read straight from Databricks Apps proxy headers at 25 call
sites — ``x-forwarded-email``, ``x-forwarded-preferred-username``,
``x-forwarded-access-token``. That works only behind the Apps proxy, so a
container deployment had no identity at all, and every site had its own
fallback behaviour.

Resolution order, first hit wins:

1. **Server-side session** — set by the OIDC callback. The portable path: the
   app performs the authorization-code flow itself and stores the result.
2. **Proxy headers** — still honoured so a Databricks Apps deployment keeps
   working unchanged during the transition.
3. **SCIM ``/Me``** — local development with a PAT or CLI profile, where there
   is no proxy and no login. Resolves the developer's own identity so audit
   attribution (review sign-offs, status changes) records a real actor instead
   of an empty string.

Call sites take an :class:`Identity` and never touch headers, which is what
lets the source change without touching them again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from back.core.logging import get_logger

logger = get_logger(__name__)

#: Session keys written by the OIDC callback.
SESSION_EMAIL = "auth_email"
SESSION_NAME = "auth_display_name"
SESSION_TOKEN = "auth_access_token"
SESSION_GROUPS = "auth_groups"

_HEADER_EMAIL = "x-forwarded-email"
_HEADER_USERNAME = "x-forwarded-preferred-username"
_HEADER_TOKEN = "x-forwarded-access-token"


@dataclass(frozen=True)
class Identity:
    """The authenticated caller.

    ``email`` is the stable key every permission decision and audit record uses.
    ``access_token`` is a Databricks user token when one is available, enabling
    per-user Unity Catalog enforcement on interactive queries; it is empty when
    the caller authenticated without one, and callers must cope with that rather
    than assume it is present.
    """

    email: str = ""
    display_name: str = ""
    access_token: str = ""
    groups: list[str] = field(default_factory=list)
    source: str = "none"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.email)

    @property
    def label(self) -> str:
        """Best available human-readable name."""
        return self.display_name or self.email


class IdentityResolver:
    """Resolve an :class:`Identity` from a request."""

    @staticmethod
    def _session_dict(request: Any) -> dict | None:
        """Return the session mapping, or *None* when there is none.

        OntoBricks uses its own :class:`FileSessionMiddleware`, which attaches
        the session to ``request.state.session``. Starlette's own
        ``request.session`` property is also consulted, but it *asserts* when
        ``SessionMiddleware`` is absent — so the access itself has to be
        guarded, not just the lookup.
        """
        state = getattr(request, "state", None)
        if state is not None:
            data = getattr(state, "session", None)
            if isinstance(data, dict):
                return data
        try:
            data = request.session
        except Exception:  # noqa: BLE001 — raising property when unmounted
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def from_session(request: Any) -> Identity | None:
        """Read an identity established by the OIDC callback."""
        session = IdentityResolver._session_dict(request)
        if not session:
            return None
        email = str(session.get(SESSION_EMAIL) or "").strip()
        if not email:
            return None
        try:
            groups = list(session.get(SESSION_GROUPS) or [])
        except Exception:  # noqa: BLE001
            groups = []
        return Identity(
            email=email,
            display_name=str(session.get(SESSION_NAME) or "").strip(),
            access_token=str(session.get(SESSION_TOKEN) or "").strip(),
            groups=[str(g) for g in groups],
            source="session",
        )

    @staticmethod
    def from_headers(request: Any) -> Identity | None:
        """Read an identity from Databricks Apps proxy headers."""
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        email = str(headers.get(_HEADER_EMAIL, "") or "").strip()
        username = str(headers.get(_HEADER_USERNAME, "") or "").strip()
        if not email and not username:
            return None
        return Identity(
            email=email or username,
            display_name=username or email,
            access_token=str(headers.get(_HEADER_TOKEN, "") or "").strip(),
            source="proxy_headers",
        )

    @staticmethod
    def from_local_workspace() -> Identity | None:
        """Resolve the developer's own identity via SCIM ``/Me``."""
        try:
            from back.core.databricks import get_local_user_email

            email = str(get_local_user_email() or "").strip()
        except Exception as exc:  # noqa: BLE001 — no workspace reachable
            logger.debug("Local identity lookup failed: %s", exc)
            return None
        if not email:
            return None
        return Identity(email=email, display_name=email, source="local_workspace")

    @classmethod
    def resolve(cls, request: Any, *, allow_local: bool = True) -> Identity:
        """Return the caller's identity, or an empty one when anonymous.

        ``allow_local`` is disabled when authentication is enforced: falling
        back to the deploying principal's own SCIM identity would silently
        authenticate every anonymous request as that user.
        """
        for source in (cls.from_session, cls.from_headers):
            identity = source(request)
            if identity is not None:
                return identity
        if allow_local:
            identity = cls.from_local_workspace()
            if identity is not None:
                return identity
        return Identity()


def identity_of(request: Any) -> Identity:
    """Return the caller's identity, preferring what the middleware resolved.

    ``PermissionMiddleware`` resolves identity once per request and stores it on
    ``request.state.identity``. Call sites go through this so they neither
    re-resolve (which would re-hit SCIM) nor read headers directly. Falls back to
    a fresh resolve for requests that bypass the middleware — health probes and
    the mounted external API among them.
    """
    state = getattr(request, "state", None)
    existing = getattr(state, "identity", None) if state is not None else None
    if isinstance(existing, Identity):
        return existing
    return IdentityResolver.resolve(request)
