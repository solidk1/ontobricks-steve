"""Shared tuning constants for PostgreSQL technical access.

Cold-start classification, retry backoff, pool sizing and ``application_name``
labels. All of it is server-agnostic: the same values apply to Azure Database for
PostgreSQL, RDS, a self-hosted server, or Databricks Lakebase.

These lived under ``back/core/databricks/lakebase/`` because Lakebase was once the
only Postgres target. Three of the four consumers — the shared connection pool,
the graph-db pool and the registry store — are server-agnostic, so the module had
the generic Postgres layer importing out of a vendor package. It now sits beside
:class:`back.core.postgres.PostgresAuth` and :class:`PostgresConnectionPool`,
which is where P2b put that layer.
"""

from __future__ import annotations

# SQLSTATEs used to classify connection failures during ``_open_one``.
COLD_START_SQLSTATES = frozenset({"57P03"})  # cannot_connect_now (scale-to-zero)
AUTH_FAILURE_SQLSTATES = frozenset({"28P01"})  # invalid_password / token expired

# Cold-start retry. Databricks Lakebase scales to zero when idle; other servers
# can be briefly unreachable during failover. The same backoff covers both.
MAX_COLD_START_ATTEMPTS = 6
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 16.0

# Connection-pool tuning. ``POOL_MAX_LIFETIME_S`` sits comfortably below the
# shortest credential lifetime any supported auth mode issues (a Lakebase JWT or
# an Entra token, both ~1 h) so a connection is always retired before its
# credentials could expire mid-query. ``POOL_MAX_SIZE`` is small on purpose: both
# workloads are admin/low-concurrency, and Postgres connections are not cheap.
POOL_MAX_SIZE = 4
POOL_MAX_LIFETIME_S = 45 * 60.0  # 45 min
POOL_ACQUIRE_TIMEOUT_S = 30.0

# Credential (Postgres password) lifetime — refresh ~5 min before the 1 h expiry.
# Read by LakebaseAuth for its JWT; EntraCredential carries its own 300s margin.
TOKEN_TTL_S = 3300

# libpq ``application_name`` labels used for connection tracing. They select
# the per-workload pool namespace but are otherwise cosmetic.
APPLICATION_NAME_REGISTRY = "ontobricks-registry"
APPLICATION_NAME_GRAPH = "ontobricks-graphdb"
