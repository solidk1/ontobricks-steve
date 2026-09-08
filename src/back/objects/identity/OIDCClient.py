"""OAuth 2.0 authorization-code flow with PKCE against Databricks.

Databricks Apps injected the user's identity as proxy headers. Off-platform the
app performs the flow itself, against a Databricks **custom OAuth app
integration** registered in the account console with this deployment's redirect
URI. Databricks is therefore the identity provider, and one login yields the
user's email, their groups, and a Databricks user access token — the last of
which is what enables per-user Unity Catalog enforcement on interactive queries.

Endpoints are derived from ``ONTOBRICKS_OIDC_HOST``, falling back to
``DATABRICKS_HOST``:

* authorize — ``{host}/oidc/v1/authorize``
* token     — ``{host}/oidc/v1/token``
* identity  — SCIM ``/api/2.0/preview/scim/v2/Me``

The two are separable because they are often **different hosts**. A custom app
integration registered in the *account* console is issued by the account host,
whereas Unity Catalog and SQL warehouse calls must go to the *workspace* host
(``adb-<id>.<n>.azuredatabricks.net``). Some Azure deployments also front the
account with a vanity domain that answers OIDC discovery but returns HTTP 303
for every workspace API path. Pointing ``DATABRICKS_HOST`` at such a host to
make login work therefore broke warehouse listing, and pointing it at the
workspace broke login. Set ``ONTOBRICKS_OIDC_HOST`` to wherever the app
integration lives and leave ``DATABRICKS_HOST`` as the workspace.

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
        self.host = (
            _env("ONTOBRICKS_OIDC_HOST") or _env("DATABRICKS_HOST")
        ).rstrip("/")
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
            missing.append("ONTOBRICKS_OIDC_HOST (or DATABRICKS_HOST)")
        if not self.client_id:
            missing.append("ONTOBRICKS_OIDC_CLIENT_ID")
        if not self.redirect_uri:
            missing.append("ONTOBRICKS_OIDC_REDIRECT_URI")
        return missing

    @property
    def workspace_host(self) -> str:
        """Host for **workspace** REST calls (SCIM, UC, warehouses).

        Distinct from :attr:`host`, which is the OIDC issuer. An account-level
        or vanity host answers OIDC discovery but returns HTTP 303 for every
        ``/api/2.0/...`` path, so SCIM has to go to the workspace explicitly.
        Falls back to the issuer host when only one is configured, which is the
        single-host case where they are the same thing anyway.
        """
        host = (_env("DATABRICKS_HOST") or self.host).rstrip("/")
        if host and not host.startswith("http"):
            host = f"https://{host}"
        return host

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

    @staticmethod
    def _email_from_token(access_token: str) -> str:
        """Read the subject e-mail out of the access token's claims.

        Databricks issues a JWT whose ``sub`` is the user's e-mail, so the
        identity is already in hand after the code exchange and needs no second
        round-trip.

        The claims are **decoded, not verified**, which is safe only because of
        where this token came from: we received it directly from the Databricks
        token endpoint over TLS, in exchange for our client secret plus the PKCE
        verifier we generated. It is not a bearer token presented by the caller,
        so there is no attacker-controlled path into this value. Verifying the
        signature would mean fetching and caching JWKS to re-prove something the
        exchange already established.

        Returns "" for an opaque or unparsable token, so the caller falls back to
        SCIM.
        """
        import base64
        import json as _json

        parts = (access_token or "").split(".")
        if len(parts) != 3:
            return ""
        try:
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload))
        except Exception:  # noqa: BLE001
            return ""
        sub = str(claims.get("sub") or "").strip()
        return sub if "@" in sub else ""

    def fetch_identity(self, access_token: str) -> dict[str, Any]:
        """Resolve email, display name and groups for the signed-in user.

        The e-mail comes from the access token's ``sub`` claim. SCIM ``/Me`` is
        then consulted only to enrich display name and group memberships, and a
        failure there is **not fatal** — groups feed group-based app-role grants,
        so a user holding a direct grant still gets in.

        This used to depend on SCIM for the e-mail too, which made the whole
        login fail closed on any SCIM hiccup: a live deployment returned
        "Could not resolve the signed-in user via SCIM /Me" and there was no way
        past it, even though the token in hand already carried the identity.
        """
        import requests

        email = self._email_from_token(access_token)
        me: dict[str, Any] = {}
        try:
            resp = requests.get(
                f"{self.workspace_host}/api/2.0/preview/scim/v2/Me",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": HTTP_USER_AGENT,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            me = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            if not email:
                # No token claim and no SCIM: genuinely cannot identify them.
                # Carry the underlying error, which the previous message hid.
                raise InfrastructureError(
                    "Could not resolve the signed-in user: the access token "
                    "carried no 'sub' claim and SCIM /Me failed",
                    detail=str(exc)[:500],
                ) from exc
            logger.warning(
                "SCIM /Me failed for %s; continuing with token claims only "
                "(groups will be empty, so only direct app-role grants apply): %s",
                email,
                str(exc)[:200],
            )

        if not email:
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
