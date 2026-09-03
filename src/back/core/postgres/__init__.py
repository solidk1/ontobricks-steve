"""Generic PostgreSQL access layer.

OntoBricks stores its registry and (on the Postgres engine) its triple store in
an ordinary PostgreSQL database — by default Azure Database for PostgreSQL —
rather than in Lakebase. This package owns the connection identity for that
server; the pool itself still lives in
:mod:`back.core.postgres.PostgresConnectionPool` and is shared, since
its LIFO reuse, cold-start backoff and retry-once-on-auth-failure behaviour are
exactly what Entra token rotation requires.
"""

from back.core.postgres.EntraCredential import (  # noqa: F401
    OSSRDBMS_SCOPE,
    EntraCredential,
)
from back.core.postgres.PostgresAuth import (  # noqa: F401
    AUTH_ENTRA,
    AUTH_PASSWORD,
    PostgresAuth,
)

__all__ = [
    "EntraCredential",
    "OSSRDBMS_SCOPE",
    "PostgresAuth",
    "AUTH_ENTRA",
    "AUTH_PASSWORD",
]
