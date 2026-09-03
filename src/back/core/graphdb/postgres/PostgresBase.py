"""Shared wiring for Lakebase Postgres graph backends."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from back.core.errors import InfrastructureError
from back.core.graphdb.GraphDBBackend import GraphDBBackend
from back.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_GRAPH_SCHEMA = "ontobricks_graph"

_SAFE_SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def default_schema() -> str:
    """Default Postgres schema name for Lakebase graph triple tables."""
    return DEFAULT_GRAPH_SCHEMA


def resolve_postgres_database_override(cfg: Optional[Dict[str, Any]]) -> str:
    """Return Lakebase's Postgres ``database`` override from engine config.

    Accepts a Lakebase section, a nested ``graph_engine_config`` root, or a
    legacy flat blob. Nested storage keeps backends fully separated; the
    flat-era heuristic (ignore ``database: "neo4j"`` when a Neo4j URI /
    ``neo4j_database`` is also present) remains for unmigrated saves.
    """
    data = cfg if isinstance(cfg, dict) else {}
    if isinstance(data.get("lakebase"), dict) or isinstance(data.get("neo4j"), dict):
        from back.core.graphdb.engine_config import lakebase_section

        data = lakebase_section(data)
        return str(data.get("database") or "").strip()
    raw = str(data.get("database") or "").strip()
    if not raw:
        return ""
    if raw.lower() == "neo4j" and (
        str(data.get("uri") or "").strip()
        or str(data.get("neo4j_database") or "").strip()
    ):
        return ""
    return raw


def validate_graph_schema(name: str) -> str:
    """Return a safe Postgres schema name or raise ValueError."""
    s = (name or "").strip() or DEFAULT_GRAPH_SCHEMA
    if not _SAFE_SCHEMA_RE.match(s):
        raise ValueError(f"Invalid Postgres schema identifier: {s!r}")
    return s


#: Keys that configured the removed Lakeflow managed-synced mode. Existing
#: stored configs still carry them, so they are accepted and ignored rather than
#: rejected — rejecting would make a saved config fail validation and block Save.
_IGNORED_LEGACY_SYNC_KEYS = (
    "sync_mode",
    "sync_table_mode",
    "sync_timeout_s",
    "sync_uc_catalog",
    "sync_uc_schema",
)


def validate_engine_config_keys(config: Dict[str, Any]) -> Tuple[bool, str]:
    """Validate optional Postgres ``graph_engine_config`` keys.

    Recognised keys:

    * ``database`` -- override the Postgres database name (str).
    * ``schema``   -- fallback graph schema name when **Settings → Registry**
      has no Volume schema; otherwise ``RegistryCfg.schema`` always wins for
      Postgres (it drives ``search_path``).

    Unknown keys pass through silently so admin-only feature flags can be
    layered on without forcing a schema migration. That includes
    :data:`_IGNORED_LEGACY_SYNC_KEYS`, left over from the removed managed-synced
    mode; they are logged once at debug level and otherwise have no effect.
    """
    db = config.get("database", None)
    if db is not None and not isinstance(db, str):
        return False, "graph_engine_config.database must be a string"
    sch = config.get("schema", None)
    if sch is not None:
        if not isinstance(sch, str):
            return False, "graph_engine_config.schema must be a string"
        try:
            validate_graph_schema(sch)
        except ValueError as exc:
            return False, str(exc)
    stale = [k for k in _IGNORED_LEGACY_SYNC_KEYS if k in config]
    if stale:
        logger.debug(
            "graph_engine_config carries keys from the removed managed-synced "
            "mode; ignoring: %s",
            ", ".join(stale),
        )
    return True, ""


def _require_psycopg():
    from back.core.graphdb.postgres.pool import _require_psycopg as _rq

    return _rq()


class PostgresBase(GraphDBBackend):
    """Connection flags + naming shared by Lakebase graph stores."""

    def __init__(
        self,
        auth: Any,
        schema: str,
        database_override: str = "",
    ) -> None:
        self._auth = auth
        self._schema = validate_graph_schema(schema)
        self._database_override = database_override or ""

    # -- GraphDBBackend / TripleStore naming --------------------------------

    @staticmethod
    def physical_table_id(name: str) -> str:
        """Lower-case safe SQL identifier for the triple table."""
        from back.core.helpers import safe_identifier

        base = name.split(".")[-1] if "." in name else name
        return (safe_identifier(base) or "triples").lower()

    def _sql_relation(self, table_name: str) -> str:
        return self.physical_table_id(table_name)

    def get_node_table(self, table_name: str) -> str:
        return self.physical_table_id(table_name)

    # -- Capability / connection --------------------------------------------

    @property
    def supports_cypher(self) -> bool:
        return False

    @property
    def query_dialect(self) -> str:
        return "sql"

    def get_query_translator(self, table_name: str = "") -> Any:
        from back.core.reasoning.SWRLSQLTranslator import SWRLSQLTranslator

        return SWRLSQLTranslator()

    def get_connection(self) -> Any:
        raise InfrastructureError(
            "Lakebase graph backend does not expose a native driver connection — "
            "use execute_query()."
        )

    def close(self) -> None:
        return

    def local_path(self) -> Optional[str]:
        return None

    def remote_archive_path(self, uc_domain_path: str) -> Optional[str]:
        return None

    # -- Pool helpers -------------------------------------------------------

    @property
    def graph_schema(self) -> str:
        """Postgres schema for triple tables.

        At runtime :class:`~back.core.graphdb.GraphDBFactory` normally sets this from
        **Settings → Registry** Volume ``catalog.<schema>.volume`` when that schema is
        non-empty, overriding ``graph_engine_config.schema`` so managed-sync UC names
        stay aligned with the registry UC namespace.

        In managed-sync mode this identifier is also the **middle segment** of the
        Unity Catalog name for the Lakeflow synced table
        (``<catalog>.<this_schema>.<table>``).
        """
        return self._schema

    def _pool(self) -> Any:
        from back.core.graphdb.postgres.pool import get_postgres_graph_pool

        return get_postgres_graph_pool(self._auth, self._schema, self._database_override)

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        _, dict_row = _require_psycopg()
        pool = self._pool()
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f'SET search_path TO "{self._schema}"')
                yield cur
