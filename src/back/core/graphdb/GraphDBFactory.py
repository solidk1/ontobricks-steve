"""Factory for creating graph database backends from domain session configuration.

A single entry point (:meth:`GraphDBFactory.create` / :func:`get_graphdb`)
constructs the graph DB backend for a domain:

* ``engine=None`` — auto-resolve the engine from global/registry config
  (``lakebase`` by default, ``delta`` when ``triple_store_backend`` is
  ``databricks``).  This is the common path callers use.
* ``engine="postgres"`` — flat triple tables on PostgreSQL.
* ``engine="delta"`` — materialized Delta triple tables in Unity Catalog.
* ``engine="view"`` — a raw, read-only Delta store bound to a SQL warehouse
  (health probes against a UC view/table).

New engines are pluggable — copy ``_starter_kit/`` into
``back/core/graphdb/<engine>/`` and register a ``_create_<engine>`` branch.
The *engine_config* JSON is engine-specific (admin: Settings → Graph DB).
"""

from typing import Any

from back.core.logging import get_logger

logger = get_logger(__name__)

# Unified per-domain graph backend vocabulary.  A domain stores exactly one of
# these in ``DomainSession.info['graph_backend']`` (mandatory).  Each value maps
# to a ``triple_store_backend`` + ``graph_engine`` pair used internally by the
# factory:
#   ``lakebase``   -> triple_store_backend=lakebase,  graph_engine=lakebase
#   ``databricks`` -> triple_store_backend=databricks (Delta)
#   ``neo4j``      -> triple_store_backend=lakebase,  graph_engine=neo4j
GRAPH_BACKENDS: tuple[str, ...] = ("postgres", "databricks", "neo4j")
DEFAULT_GRAPH_BACKEND = "postgres"

#: Values a stored domain may still carry, mapped to the canonical name.
#: ``graph_backend`` lives in the persisted domain JSON, so pre-0.8 documents
#: say ``lakebase``; they are read, normalised, and rewritten as ``postgres``
#: on the next save.
_LEGACY_GRAPH_BACKENDS = {"lakebase": "postgres"}


def normalize_graph_backend(value: str | None) -> str:
    """Return a valid per-domain graph backend, defaulting to ``postgres``.

    Accepts the legacy ``lakebase`` spelling so a domain saved before the
    rename still selects the Postgres engine rather than silently falling back
    to the default (which happens to be the same engine today, but would not be
    if the default ever changed).
    """
    v = (value or "").strip().lower()
    v = _LEGACY_GRAPH_BACKENDS.get(v, v)
    return v if v in GRAPH_BACKENDS else DEFAULT_GRAPH_BACKEND


class GraphDBFactory:
    """Construct graph DB backend instances from domain session configuration."""

    POSTGRES_AVAILABLE = False
    NEO4J_AVAILABLE = False

    def create(
        self,
        domain: Any,
        settings: Any | None = None,
        engine: str | None = None,
        engine_config: dict[str, Any] | None = None,
    ) -> Any | None:
        """Create a graph DB backend.

        Args:
            domain: Domain session with info and databricks config.
            settings: Optional application settings.
            engine: One of ``None`` (auto-resolve from config), ``"postgres"``,
                    ``"delta"``, or ``"view"`` (raw read-only Delta store).
            engine_config: Engine-specific JSON configuration set by the
                           admin in Settings > Graph DB.

        Returns:
            GraphDBBackend instance or *None* if configuration is incomplete.
        """
        if engine is None:
            return self._create_auto(domain, settings)

        if engine == "view":
            return self._create_delta_view(domain, settings)

        if engine_config is None:
            engine_config = {}

        from back.core.graphdb.engine_config import neo4j_section, postgres_section

        if engine in ("postgres", "lakebase"):
            return self._create_lakebase(
                domain, settings, engine_config=postgres_section(engine_config)
            )

        if engine == "neo4j":
            return self._create_neo4j(
                domain, settings, engine_config=neo4j_section(engine_config)
            )

        if engine == "delta":
            return self._create_delta(domain, settings)

        logger.warning("Unknown graph DB engine: %s", engine)
        return None

    def _create_neo4j(
        self,
        domain: Any,
        settings: Any | None = None,
        *,
        engine_config: dict[str, Any] | None = None,
    ) -> Any | None:
        """Instantiate :class:`Neo4jStore` against a named Settings connection.

        Resolves ``domain.info.neo4j_connection`` against
        ``engine_config.connections`` (or a nested graph_engine_config root).
        The matched profile supplies URI / database / auth fields.
        """
        try:
            from back.core.graphdb.engine_config import (
                resolve_neo4j_connection,
            )
            from back.core.graphdb.neo4j import NEO4J_AVAILABLE
            from back.core.graphdb.neo4j.Neo4jStore import Neo4jStore
            from shared.config.constants import DEFAULT_GRAPH_NAME
        except ImportError as e:
            logger.warning("Neo4j graph engine requires the 'neo4j' driver: %s", e)
            return None

        if not NEO4J_AVAILABLE:
            logger.warning("Neo4j graph backend unavailable (neo4j driver not installed)")
            return None

        info = domain.info or {}
        conn_name = str(info.get("neo4j_connection") or "").strip()
        if not conn_name:
            logger.warning(
                "Neo4jStore: domain has no neo4j_connection — pick one in "
                "Domain → Information → Knowledge Graph"
            )
            return None

        # engine_config may be the neo4j section or the nested root.
        root_or_section = engine_config if isinstance(engine_config, dict) else {}
        profile = resolve_neo4j_connection(root_or_section, conn_name)
        if not profile and (
            "postgres" in root_or_section
            or "lakebase" in root_or_section
            or "neo4j" in root_or_section
            or "connections" not in root_or_section
        ):
            # When create() already passed neo4j_section(...), wrap it so
            # resolve_neo4j_connection can still see connections[].
            profile = resolve_neo4j_connection({"neo4j": root_or_section}, conn_name)
        if not profile:
            logger.warning(
                "Neo4jStore: connection %r not found in Settings → Neo4j",
                conn_name,
            )
            return None

        cfg = dict(profile)
        base_name = info.get("name", DEFAULT_GRAPH_NAME)
        version = getattr(domain, "current_version", "1") or "1"
        db_name = "%s_V%s" % (base_name, version)
        try:
            return Neo4jStore(db_name=db_name, engine_config=cfg)
        except (ValueError, NotImplementedError) as exc:
            logger.warning("Neo4jStore configuration error: %s", exc)
            return None
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to create Neo4jStore: %s", e)
            return None

    def _create_auto(
        self, domain: Any, settings: Any | None = None
    ) -> Any | None:
        """Resolve the engine from global/registry config and dispatch.

        Mirrors the former ``TripleStoreFactory`` ``backend="graph"`` behaviour.
        """
        ts_backend = self._resolve_triple_store_backend(domain, settings)
        if ts_backend == "databricks":
            return self.create(domain, settings, engine="delta", engine_config={})

        engine = self._resolve_graph_engine(domain, settings) or "postgres"
        engine_config = self._resolve_graph_engine_config(domain, settings)
        return self.create(
            domain, settings, engine=engine, engine_config=engine_config or {}
        )

    # ------------------------------------------------------------------
    # Config resolution (formerly on TripleStoreFactory)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_global_config(domain: Any, settings: Any | None, accessor, *, force: bool = False):
        """Call *accessor(global_config_service, host, token, registry_cfg)*.

        Returns ``None`` on any error (registry not configured, etc.).

        When *force* is ``True`` the ``GlobalConfigService`` in-memory cache is
        bypassed and a fresh read is performed against the backing store.  This
        is important for build-time resolution: the cache may hold the empty
        template (``_empty()``) from a cold-start race where Lakebase was
        briefly unavailable, while the Settings UI correctly shows the saved
        value because it always uses ``force=True``.  Without bypassing the
        cache a build could silently resolve against an empty engine config.
        """
        try:
            from back.core.helpers import get_databricks_host_and_token
            from back.objects.session.GlobalConfigService import global_config_service

            if settings is not None:
                host, token = get_databricks_host_and_token(domain, settings)
            else:
                db = getattr(domain, "databricks", None) or {}
                host = db.get("host", "")
                token = db.get("token", "")
            from back.objects.registry import RegistryCfg

            registry_cfg = RegistryCfg.from_domain(domain, settings).as_dict()
            if force:
                global_config_service.load(host, token, registry_cfg, force=True)
            return accessor(global_config_service, host, token, registry_cfg)
        except Exception as exc:
            logger.debug("Could not read global config: %s", exc)
            return None

    @staticmethod
    def _resolve_graph_backend(domain: Any) -> str:
        """Return the mandatory per-domain graph backend.

        The choice lives in ``DomainSession.info['graph_backend']`` (set from the
        Domain Information -> Knowledge Graph tab) and is versioned with the
        domain.  Missing/invalid values default to ``lakebase`` so pre-existing
        domains keep working.
        """
        try:
            info = getattr(domain, "info", None)
            if isinstance(info, dict):
                return normalize_graph_backend(info.get("graph_backend"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read per-domain graph_backend: %s", exc)
        return DEFAULT_GRAPH_BACKEND

    @staticmethod
    def _resolve_graph_engine(
        domain: Any, settings: Any | None = None, *, force: bool = False
    ) -> str | None:
        """Resolve the graph engine from the per-domain backend choice.

        ``settings``/``force`` are accepted for call-site compatibility but no
        longer consulted — the selection is purely per-domain now.
        """
        backend = GraphDBFactory._resolve_graph_backend(domain)
        return "neo4j" if backend == "neo4j" else "postgres"

    @staticmethod
    def _resolve_triple_store_backend(
        domain: Any, settings: Any | None = None, *, force: bool = False
    ) -> str:
        """Resolve the triple-store backend from the per-domain backend choice."""
        backend = GraphDBFactory._resolve_graph_backend(domain)
        return "databricks" if backend == "databricks" else "postgres"

    @staticmethod
    def _resolve_graph_engine_config(
        domain: Any, settings: Any | None = None, *, force: bool = False
    ) -> dict | None:
        """Read the engine-specific connection JSON config from ``GlobalConfigService``.

        Engine *connection* configuration (Neo4j Bolt creds, Lakebase schema /
        sync options) remains workspace-global — only the backend *selection*
        moved per-domain.  Pass *force=True* to bypass the in-memory cache.
        """
        raw = GraphDBFactory._read_global_config(
            domain,
            settings,
            lambda gcs, h, t, r: gcs.get_graph_engine_config(h, t, r),
            force=force,
        )
        return raw if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------
    # Engine constructors
    # ------------------------------------------------------------------

    def _create_delta_view(
        self, domain: Any, settings: Any | None = None
    ) -> Any | None:
        """Instantiate a raw, read-only :class:`DeltaFlatStore` on a SQL warehouse.

        Bound with ``domain=None`` so it operates directly on the FQNs passed in
        (health probes against a UC view/table).  Formerly ``backend="view"``.
        """
        try:
            from back.core.databricks import (
                DatabricksClient,
                has_implicit_credentials,
            )
            from back.core.graphdb.delta.DeltaFlatStore import DeltaFlatStore
            from back.core.helpers import (
                get_databricks_host_and_token,
                resolve_delta_warehouse_id,
            )

            if settings is not None:
                host, token = get_databricks_host_and_token(domain, settings)
                warehouse_id = resolve_delta_warehouse_id(domain, settings)
            else:
                db = domain.databricks or {}
                host = db.get("host", "")
                token = db.get("token", "")
                warehouse_id = ""
            if not host and not has_implicit_credentials():
                logger.warning("Delta view store: missing host")
                return None
            if not token and not has_implicit_credentials():
                logger.warning("Delta view store: missing token")
                return None
            if not warehouse_id:
                logger.warning("Delta view store: missing sql_warehouse_id")
                return None
            client = DatabricksClient(
                host=host,
                token=token,
                warehouse_id=warehouse_id,
            )
            return DeltaFlatStore(client)
        except Exception as e:
            logger.exception("Failed to create Delta view store: %s", e)
            return None

    def _create_lakebase(
        self,
        domain: Any,
        settings: Any | None = None,
        *,
        engine_config: dict[str, Any] | None = None,
    ) -> Any | None:
        """Instantiate :class:`PostgresFlatStore` on the bound Lakebase instance."""
        try:
            from back.core.databricks import get_graph_auth
            from back.core.graphdb.postgres import POSTGRES_AVAILABLE
            from back.core.graphdb.postgres.PostgresBase import (
                resolve_postgres_database_override,
            )
            from back.core.graphdb.postgres.PostgresFlatStore import (
                PostgresFlatStore,
                resolve_postgres_graph_schema,
            )
        except ImportError as e:
            logger.warning("Lakebase graph engine requires psycopg: %s", e)
            return None

        if not POSTGRES_AVAILABLE:
            logger.warning("Lakebase graph backend unavailable (psycopg not installed)")
            return None

        cfg = engine_config or {}
        schema_raw = (cfg.get("schema") or "").strip()
        database_override = resolve_postgres_database_override(cfg)

        try:
            schema = resolve_postgres_graph_schema(domain, settings, str(schema_raw))
        except ValueError as exc:
            logger.warning("Invalid lakebase graph schema: %s", exc)
            return None

        branch_path = str(cfg.get("lakebase_branch") or "").strip()
        try:
            auth = get_graph_auth(branch_path, database_override)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lakebase auth unavailable for graph engine: %s", exc)
            return None

        if not getattr(auth, "is_available", False):
            logger.warning(
                "Lakebase graph engine selected but PGHOST/PGUSER are not configured"
                " (branch=%r)",
                branch_path or "<bound>",
            )
            return None

        try:
            return PostgresFlatStore(
                auth,
                schema=schema,
                database_override=database_override,
            )
        except Exception as e:
            logger.exception("Failed to create Lakebase graph store: %s", e)
            return None

    def _create_delta(
        self,
        domain: Any,
        settings: Any | None = None,
    ) -> Any | None:
        """Instantiate :class:`DeltaFlatStore` on SQL Warehouse."""
        try:
            from back.core.graphdb.delta.DeltaBase import create_databricks_client
            from back.core.graphdb.delta.DeltaFlatStore import DeltaFlatStore
        except ImportError as exc:
            logger.warning("Delta graph engine unavailable: %s", exc)
            return None

        client = create_databricks_client(domain, settings)
        if client is None:
            return None
        try:
            return DeltaFlatStore(client, domain=domain, settings=settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create DeltaFlatStore: %s", exc)
            return None


    @classmethod
    def get_graphdb(
        cls,
        domain: Any,
        settings: Any | None = None,
        engine: str | None = None,
        engine_config: dict[str, Any] | None = None,
    ) -> Any | None:
        """Convenience wrapper using the package singleton factory instance."""
        return _get_factory_singleton().create(
            domain,
            settings=settings,
            engine=engine,
            engine_config=engine_config,
        )


_factory_singleton: GraphDBFactory | None = None


def _get_factory_singleton() -> GraphDBFactory:
    global _factory_singleton
    if _factory_singleton is None:
        _factory_singleton = GraphDBFactory()
    return _factory_singleton


try:
    from back.core.graphdb.postgres import POSTGRES_AVAILABLE as _LB_AVAIL  # noqa: F401

    GraphDBFactory.POSTGRES_AVAILABLE = bool(_LB_AVAIL)
except ImportError:
    logger.debug("Lakebase graph backends not available (optional dependency)")

try:
    from back.core.graphdb.neo4j import NEO4J_AVAILABLE as _NEO4J_AVAIL  # noqa: F401

    GraphDBFactory.NEO4J_AVAILABLE = bool(_NEO4J_AVAIL)
except ImportError:
    logger.debug("Neo4j graph backend not available (optional dependency)")
