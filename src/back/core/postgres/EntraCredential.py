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
"""

from __future__ import annotations

import threading
import time
from typing import Any

from back.core.errors import InfrastructureError
from back.core.logging import get_logger

logger = get_logger(__name__)

#: Audience for Azure Database for PostgreSQL / MySQL Entra authentication.
OSSRDBMS_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

#: Re-mint this many seconds before the token's own expiry. Generous, because a
#: token that expires mid-handshake fails the connection outright.
REFRESH_MARGIN_S = 300


class EntraCredential:
    """Mints and caches Entra access tokens for Postgres authentication."""

    def __init__(self, credential: Any | None = None, scope: str = OSSRDBMS_SCOPE):
        """Args:
        credential: An ``azure.identity`` credential. Defaults to
            ``DefaultAzureCredential``, constructed lazily so that importing
            this module never requires Azure configuration (tests and
            non-Azure deployments import it freely).
        scope: Token audience. Only overridden in tests.
        """
        self._credential = credential
        self._scope = scope
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
