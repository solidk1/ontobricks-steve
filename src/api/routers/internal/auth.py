"""OIDC login / logout routes.

Replaces the Databricks Apps proxy as the source of identity. The flow is
authorization-code + PKCE against Databricks as the IdP (see
:class:`back.objects.identity.OIDCClient`); on success the identity and the
user's Databricks access token land in the server-side session, where
:class:`back.objects.identity.IdentityResolver` picks them up.

Routes are deliberately exempt from the permission middleware — you cannot
require a login in order to log in — which is why ``/auth`` is in
``_PERM_BYPASS_PREFIXES``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from back.core.errors import OntoBricksError
from back.core.logging import get_logger
from back.objects.identity import IdentityResolver, OIDCClient
from back.objects.identity.IdentityResolver import (
    SESSION_EMAIL,
    SESSION_GROUPS,
    SESSION_NAME,
    SESSION_TOKEN,
)
from back.objects.identity.OIDCClient import (
    SESSION_REFRESH,
    SESSION_RETURN_TO,
    SESSION_STATE,
    SESSION_VERIFIER,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SAFE_DEFAULT_RETURN = "/"


def _session(request: Request) -> dict:
    """Return the mutable session dict, marking it dirty on write."""
    data = getattr(request.state, "session", None)
    if data is None:
        data = {}
        request.state.session = data
    request.state.session_modified = True
    return data


def _safe_return_to(raw: str) -> str:
    """Only allow same-origin relative paths, to prevent open redirect."""
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return _SAFE_DEFAULT_RETURN
    return value


@router.get("/login")
async def login(request: Request, return_to: str = "/"):
    """Begin the OIDC flow, redirecting to the Databricks authorize endpoint."""
    client = OIDCClient()
    if not client.is_configured:
        return HTMLResponse(
            "<h1>Login is not configured</h1><p>Set "
            + ", ".join(f"<code>{n}</code>" for n in client.missing_config())
            + " and restart.</p>",
            status_code=503,
        )
    url, state, verifier = client.begin()
    session = _session(request)
    session[SESSION_STATE] = state
    session[SESSION_VERIFIER] = verifier
    session[SESSION_RETURN_TO] = _safe_return_to(return_to)
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Complete the OIDC flow and establish the session."""
    if error:
        logger.warning("OIDC callback returned error=%s", error)
        return HTMLResponse(
            f"<h1>Sign-in failed</h1><p>{error}: {error_description}</p>",
            status_code=400,
        )

    session = _session(request)
    expected_state = str(session.pop(SESSION_STATE, "") or "")
    verifier = str(session.pop(SESSION_VERIFIER, "") or "")
    return_to = _safe_return_to(str(session.pop(SESSION_RETURN_TO, "") or "/"))

    # Constant-time-ish comparison is unnecessary here (state is single-use and
    # high-entropy) but an absent or mismatched state must always be fatal:
    # accepting it would allow a forged callback / login CSRF.
    if not state or not expected_state or state != expected_state:
        logger.warning("OIDC callback state mismatch")
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>Invalid or expired login state. "
            "Please start again.</p>",
            status_code=400,
        )
    if not code or not verifier:
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>Missing authorization code.</p>",
            status_code=400,
        )

    client = OIDCClient()
    try:
        tokens = client.exchange_code(code, verifier)
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            raise OntoBricksError("Token endpoint returned no access_token")
        identity = client.fetch_identity(access_token)
    except OntoBricksError as exc:
        logger.warning("OIDC sign-in failed: %s", exc)
        return HTMLResponse(f"<h1>Sign-in failed</h1><p>{exc}</p>", status_code=502)

    email = identity.get("email") or ""
    if not email:
        return HTMLResponse(
            "<h1>Sign-in failed</h1><p>Could not determine your e-mail.</p>",
            status_code=502,
        )

    session[SESSION_EMAIL] = email
    session[SESSION_NAME] = identity.get("display_name") or email
    session[SESSION_TOKEN] = access_token
    session[SESSION_GROUPS] = identity.get("groups") or []
    refresh_token = str(tokens.get("refresh_token") or "")
    if refresh_token:
        session[SESSION_REFRESH] = refresh_token

    logger.info("OIDC sign-in complete for %s", email)
    return RedirectResponse(return_to, status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear the session identity."""
    session = _session(request)
    for key in (
        SESSION_EMAIL,
        SESSION_NAME,
        SESSION_TOKEN,
        SESSION_GROUPS,
        SESSION_REFRESH,
        SESSION_STATE,
        SESSION_VERIFIER,
        SESSION_RETURN_TO,
    ):
        session.pop(key, None)
    return RedirectResponse("/", status_code=302)


@router.get("/me")
async def me(request: Request):
    """Report the resolved identity — used by the UI and for diagnostics."""
    ident = IdentityResolver.resolve(request)
    return {
        "authenticated": ident.is_authenticated,
        "email": ident.email,
        "display_name": ident.display_name,
        "groups": ident.groups,
        "source": ident.source,
        "has_databricks_token": bool(ident.access_token),
    }
