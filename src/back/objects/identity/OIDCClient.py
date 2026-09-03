"""OAuth 2.0 authorization-code flow with PKCE against Databricks.

Databricks Apps injected the user's identity as proxy headers. Off-platform the
app performs the flow itself, against a Databricks **custom OAuth app
integration** registered in the account console with this deployment's redirect
URI. Databricks is therefore the identity provider, and one login yields the
user's email, their groups, and a Databricks user access token — the last of
which is what enables per-user Unity Catalog enforcement on interactive queries.

Endpoints are derived from ``DATABRICKS_HOST``:

* authorize — ``{host}/oidc/v1/authorize``
* token     — ``{host}/oidc/v1/token``
* identity  — SCIM ``/api/2.0/preview/scim/v2/Me``

PKCE (S256) is mandatory, and ``state`` is verified on callback, so an
intercepted authorization code is useless and the callback cannot be forged
cross-site.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any
from urllib.parse import urlencode

from back.core.errors import InfrastructureError, ValidationError
from back.core.logging import get_logger
from shared.config.constants import HTTP_USER_AGENT

logger = get_logger(__name__)

#: ``all-apis`` so the resulting token can be used for UC-scoped reads;
#: ``offline_access`` for a refresh token so a session outlives one token.
DEFAULT_SCOPES = "all-apis offline_access"

SESSION_STATE = "auth_oidc_state"
SESSION_VERIFIER = "auth_oidc_verifier"
SESSION_RETURN_TO = "auth_oidc_return_to"
SESSION_REFRESH = "auth_refresh_token"

_REQUEST_TIMEOUT = 15


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class OIDCClient:
    """Authorization-code + PKCE client for Databricks as the IdP."""

    def __init__(self) -> None:
        self.host = _env("DATABRICKS_HOST").rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = f"https://{self.host}"
        self.client_id = _env("ONTOBRICKS_OIDC_CLIENT_ID")
        self.client_secret = _env("ONTOBRICKS_OIDC_CLIENT_SECRET")
        self.redirect_uri = _env("ONTOBRICKS_OIDC_REDIRECT_URI")
        self.scopes = _env("ONTOBRICKS_OIDC_SCOPES") or DEFAULT_SCOPES

    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """Whether a login can even be attempted."""
        return bool(self.host and self.client_id and self.redirect_uri)

    def missing_config(self) -> list[str]:
        """Names of the settings that still need to be provided."""
        missing = []
        if not self.host:
            missing.append("DATABRICKS_HOST")
        if not self.client_id:
            missing.append("ONTOBRICKS_OIDC_CLIENT_ID")
        if not self.redirect_uri:
            missing.append("ONTOBRICKS_OIDC_REDIRECT_URI")
        return missing

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.host}/oidc/v1/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.host}/oidc/v1/token"

    # ------------------------------------------------------------------

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        """Return ``(verifier, challenge)`` for PKCE S256."""
        verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        return verifier, challenge

    def begin(self) -> tuple[str, str, str]:
        """Start a login.

        Returns ``(authorize_url, state, verifier)``. The caller stores *state*
        and *verifier* in the session; both are required on callback.
        """
        if not self.is_configured:
            raise ValidationError(
                "OIDC login is not configured. Missing: "
                + ", ".join(self.missing_config())
            )
        state = secrets.token_urlsafe(32)
        verifier, challenge = self._pkce_pair()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.authorize_endpoint}?{urlencode(params)}", state, verifier

    def exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens."""
        import requests

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }
        auth = None
        if self.client_secret:
            # Confidential client: authenticate at the token endpoint.
            auth = (self.client_id, self.client_secret)
        try:
            resp = requests.post(
                self.token_endpoint,
                data=data,
                auth=auth,
                headers={"User-Agent": HTTP_USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json() or {}
        except Exception as exc:  # noqa: BLE001 — vendor surface
            raise InfrastructureError(
                "OIDC token exchange failed", detail=str(exc)[:500]
            ) from exc

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token for a new access token."""
        import requests

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            resp = requests.post(
                self.token_endpoint,
                data=data,
                auth=auth,
                headers={"User-Agent": HTTP_USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise InfrastructureError(
                "OIDC token refresh failed", detail=str(exc)[:500]
            ) from exc

    def fetch_identity(self, access_token: str) -> dict[str, Any]:
        """Resolve email, display name and groups via SCIM ``/Me``.

        Groups feed group-based app-role grants, so a failure to read them
        degrades to an empty list rather than aborting the login — a user with a
        direct grant can still get in.
        """
        import requests

        try:
            resp = requests.get(
                f"{self.host}/api/2.0/preview/scim/v2/Me",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": HTTP_USER_AGENT,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            me = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            raise InfrastructureError(
                "Could not resolve the signed-in user via SCIM /Me",
                detail=str(exc)[:500],
            ) from exc

        email = str(me.get("userName") or "").strip()
        if not email:
            for entry in me.get("emails") or []:
                value = str((entry or {}).get("value") or "").strip()
                if value:
                    email = value
                    break
        groups: list[str] = []
        for g in me.get("groups") or []:
            name = str((g or {}).get("display") or "").strip()
            if name:
                groups.append(name)
        return {
            "email": email,
            "display_name": str(me.get("displayName") or "").strip() or email,
            "groups": groups,
        }
