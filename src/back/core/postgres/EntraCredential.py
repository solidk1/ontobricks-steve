"""Microsoft Entra ID access tokens used as the Postgres password.

Azure Database for PostgreSQL authenticates an Entra principal by taking an
access token *in the password field*. Two properties of that make this class
necessary rather than a one-liner:

* Tokens are short-lived — Microsoft documents 5–60 minutes for this audience.
* There is no way to refresh a token inside an open session; the token is only
  read at authentication time.

So a fresh token must be minted for **every new physical connection**, which is
exactly what the connection pool's ``password()`` call gives us.

``DefaultAzureCredential`` resolves the Container Apps / AKS **managed
identity** in a deployment and the developer's ``az login`` session locally, so
one code path covers both and no database secret is ever stored.

Set ``AZURE_CLIENT_ID`` only for a *user-assigned* managed identity.

Sovereign clouds (Azure China, US Gov) need two things and no code change:
``AZURE_AUTHORITY_HOST``, which ``DefaultAzureCredential`` reads itself, and the
right token audience — derived from the ``PGHOST`` suffix by
:func:`resolve_pg_token_scope`, since the audience differs per cloud and is not a
pattern on the server name.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from back.core.errors import InfrastructureError
from back.core.logging import get_logger

logger = get_logger(__name__)

#: Entra token audience for Azure Database for PostgreSQL / MySQL, per cloud.
#:
#: The audience is **not** the same in every Azure cloud, and it is not derivable
#: from the server name by pattern — it is a fixed per-cloud constant. Getting it
#: wrong yields an ``AADSTS500011``-style rejection at connection time, which
#: reads as a credential problem rather than a wrong-cloud one.
#:
#: Keyed by the PGHOST suffix so a sovereign deployment needs no extra
#: configuration: the host already says which cloud it is in.
PG_SCOPE_BY_HOST_SUFFIX: dict[str, str] = {
    ".postgres.database.azure.com": (
        "https://ossrdbms-aad.database.windows.net/.default"
    ),
    ".postgres.database.chinacloudapi.cn": (
        "https://ossrdbms-aad.database.chinacloudapi.cn/.default"
    ),
    ".postgres.database.usgovcloudapi.net": (
        "https://ossrdbms-aad.database.usgovcloudapi.net/.default"
    ),
}

#: Audience for the global cloud, and the default when the host is unrecognised.
OSSRDBMS_SCOPE = PG_SCOPE_BY_HOST_SUFFIX[".postgres.database.azure.com"]

#: Escape hatch. Set it when a cloud is not in the table above, or when the
#: audience for one changes before this table does.
ENV_TOKEN_SCOPE = "ONTOBRICKS_PG_TOKEN_SCOPE"


def resolve_pg_token_scope(host: str = "") -> str:
    """The Entra audience to request for *host*.

    Order: an explicit ``ONTOBRICKS_PG_TOKEN_SCOPE``, then the host suffix, then
    the global default. The default is deliberate rather than an error — an
    unrecognised host is usually a self-hosted Postgres behind a CNAME, where the
    caller has set the scope explicitly or is not using Entra at all.
    """
    import os

    override = (os.environ.get(ENV_TOKEN_SCOPE) or "").strip()
    if override:
        return override

    hostname = (host or os.environ.get("PGHOST") or "").strip().lower()
    for suffix, scope in PG_SCOPE_BY_HOST_SUFFIX.items():
        if hostname.endswith(suffix):
            return scope
    return OSSRDBMS_SCOPE

#: Re-mint this many seconds before the token's own expiry. Generous, because a
#: token that expires mid-handshake fails the connection outright.
REFRESH_MARGIN_S = 300


class EntraCredential:
    """Mints and caches Entra access tokens for Postgres authentication."""

    def __init__(self, credential: Any | None = None, scope: str = ""):
        """Args:
        credential: An ``azure.identity`` credential. Defaults to
            ``DefaultAzureCredential``, constructed lazily so that importing
            this module never requires Azure configuration (tests and
            non-Azure deployments import it freely).

            ``DefaultAzureCredential`` reads ``AZURE_AUTHORITY_HOST`` itself, so
            a sovereign cloud needs that variable set but no code change here.
        scope: Token audience. Resolved from ``PGHOST`` when empty, which is what
            makes a sovereign deployment work without extra configuration.
        """
        self._credential = credential
        self._scope = scope or resolve_pg_token_scope()
        self._token: str = ""
        self._expires_on: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def _ensure_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover — azure-identity is a core dep
            raise InfrastructureError(
                "azure-identity is not installed, so Entra authentication to "
                "Postgres is unavailable. Install it, or set "
                "ONTOBRICKS_PG_AUTH=password to use PGPASSWORD instead."
            ) from exc
        self._credential = DefaultAzureCredential()
        return self._credential

    def token(self) -> str:
        """Return a valid access token, minting one when needed.

        Thread-safe: the pool opens connections from several threads and a
        stampede of token requests would be both slow and rate-limited.
        """
        with self._lock:
            now = time.time()
            if self._token and now < (self._expires_on - REFRESH_MARGIN_S):
                return self._token

            credential = self._ensure_credential()
            try:
                access = credential.get_token(self._scope)
            except Exception as exc:  # noqa: BLE001 — vendor surface
                raise InfrastructureError(
                    "Could not obtain a Microsoft Entra token for Postgres "
                    f"(scope {self._scope}). In a container check that a managed "
                    "identity is assigned; locally run `az login`. "
                    f"Underlying error: {exc}"
                ) from exc

            # Strip: a whitespace-only value is truthy but would be handed to
            # libpq and fail with an opaque authentication error. JWTs never
            # contain whitespace, so stripping is lossless.
            token = (getattr(access, "token", "") or "").strip()
            if not token:
                raise InfrastructureError(
                    "Microsoft Entra returned an empty token for Postgres."
                )
            self._token = token
            # expires_on is an absolute POSIX timestamp.
            self._expires_on = float(getattr(access, "expires_on", now + 3600))
            logger.info(
                "Entra Postgres token minted, valid for ~%d min",
                max(0, int((self._expires_on - now) / 60)),
            )
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next call mints a fresh one."""
        with self._lock:
            self._token = ""
            self._expires_on = 0.0
