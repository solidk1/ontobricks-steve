"""Connection parameters for a generic PostgreSQL server.

Implements the same surface the connection pool already expects of
:class:`~back.core.databricks.lakebase.LakebaseAuth` — ``host`` / ``port`` /
``database`` / ``user`` / ``password()`` / ``invalidate()`` / ``kwargs()`` /
``conninfo()`` — so it drops into
:class:`~back.core.databricks.lakebase.LakebaseConnectionPool` unchanged,
including the pool's retry-once-on-auth-failure loop. That loop is precisely
what Entra token rotation needs, which is why the pool is reused rather than
rewritten.

What this class does *not* do is the Lakebase-specific work: no project /
branch / endpoint discovery through the Databricks control plane, and no
Lakebase JWT minting. Connection coordinates come from the standard ``PG*``
environment variables, and the password is either an Entra access token or
``PGPASSWORD``.
"""

from __future__ import annotations

import os

from back.core.errors import ValidationError
from back.core.logging import get_logger
from back.core.postgres.EntraCredential import EntraCredential

logger = get_logger(__name__)

AUTH_ENTRA = "entra"
AUTH_PASSWORD = "password"

DEFAULT_SSLMODE = "require"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class PostgresAuth:
    """Postgres connection identity, authenticated by Entra or by password."""

    def __init__(
        self,
        auth_mode: str = "",
        credential: EntraCredential | None = None,
    ) -> None:
        """Args:
        auth_mode: ``"entra"`` or ``"password"``. Defaults to
            ``ONTOBRICKS_PG_AUTH``, then to ``entra`` — Azure's managed
            identity path is the intended production configuration, and it
            needs no stored secret.
        credential: Injected in tests; otherwise built on first use.
        """
        mode = (auth_mode or _env("ONTOBRICKS_PG_AUTH") or AUTH_ENTRA).lower()
        if mode not in (AUTH_ENTRA, AUTH_PASSWORD):
            raise ValidationError(
                f"ONTOBRICKS_PG_AUTH must be {AUTH_ENTRA!r} or {AUTH_PASSWORD!r}, "
                f"got {mode!r}"
            )
        self.auth_mode = mode
        self._credential = credential or (
            EntraCredential() if mode == AUTH_ENTRA else None
        )

    # ------------------------------------------------------------------
    # Connection coordinates — standard PG* variables only
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        host = _env("PGHOST")
        if not host:
            raise ValidationError(
                "PGHOST is not set. Point it at your PostgreSQL server, e.g. "
                "<server>.postgres.database.azure.com"
            )
        return host

    @property
    def port(self) -> int:
        raw = _env("PGPORT", "5432")
        try:
            return int(raw)
        except ValueError:
            raise ValidationError(f"PGPORT is not a number: {raw!r}") from None

    @property
    def database(self) -> str:
        db = _env("PGDATABASE")
        if not db:
            raise ValidationError(
                "PGDATABASE is not set. OntoBricks installs into an existing "
                "database as a new schema; name that database here."
            )
        return db

    @property
    def user(self) -> str:
        user = _env("PGUSER")
        if not user:
            hint = (
                "For Entra auth this is the principal name mapped with "
                "pgaadauth_create_principal (a managed identity's name, or your "
                "own UPN)."
                if self.auth_mode == AUTH_ENTRA
                else "For password auth this is the Postgres role name."
            )
            raise ValidationError(f"PGUSER is not set. {hint}")
        return user

    @property
    def sslmode(self) -> str:
        """TLS mode. Azure requires TLS, so ``require`` is the floor."""
        return _env("PGSSLMODE", DEFAULT_SSLMODE)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def password(self) -> str:
        """Return the password for a *new* physical connection.

        Under Entra this is a freshly-valid access token; the pool calls this
        once per connection it opens, which is the only correct cadence since a
        token cannot be refreshed inside an open session.
        """
        if self.auth_mode == AUTH_PASSWORD:
            pw = os.environ.get("PGPASSWORD") or ""
            if not pw:
                raise ValidationError(
                    "ONTOBRICKS_PG_AUTH=password but PGPASSWORD is not set."
                )
            return pw
        assert self._credential is not None  # guaranteed by __init__
        return self._credential.token()

    def invalidate(self) -> None:
        """Drop any cached credential so the next connection re-authenticates.

        The pool calls this when a connection attempt fails authentication,
        which is how an expired token recovers without operator intervention.
        """
        if self._credential is not None:
            self._credential.invalidate()

    # ------------------------------------------------------------------
    # psycopg plumbing — same shape the pool already consumes
    # ------------------------------------------------------------------

    def kwargs(
        self,
        *,
        application_name: str = "ontobricks",
        connect_timeout: int = 10,
    ) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password(),
            "sslmode": self.sslmode,
            "connect_timeout": connect_timeout,
            "application_name": application_name,
            # TCP keepalives: notice a server-side drop in ~25s rather than the
            # OS default ~130s, so a dead pooled connection does not stall the
            # next query for minutes.
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 5,
            "keepalives_count": 3,
        }

    def conninfo(
        self, *, application_name: str = "ontobricks", connect_timeout: int = 10
    ) -> str:
        parts = self.kwargs(
            application_name=application_name, connect_timeout=connect_timeout
        )
        return " ".join(f"{k}={v}" for k, v in parts.items())
