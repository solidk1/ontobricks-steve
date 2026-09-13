"""Readiness probe for OntoBricks — ``GET /health``.

This endpoint replaces the previous static dummy with a real
end-to-end check of every external dependency the application needs
to operate correctly:

* local filesystem — ``/tmp``, the session directory and the log
  directory must be writable, with enough free disk space;
* Databricks authentication — OAuth client-credentials in Apps mode
  or PAT in local development;
* SQL warehouse — TCP/SQL reachability via ``SELECT 1``;
* CloudFetch capability — connector prerequisites and lightweight
  runtime probe for ``use_cloud_fetch=True``;
* registry **UC volume** (binaries only) — Files-API read + write probe
  (a tiny sentinel file is written then deleted);
* registry **catalog/schema** — DDL probe via
  ``CREATE OR REPLACE VIEW <fqn> AS SELECT 1`` then ``DROP VIEW`` so
  view materialisation will succeed during Digital-Twin builds;
* **Lakebase** — connectivity/init checks plus explicit schema/table/
  sequence permission probes. When ``PG*`` env vars are unset the
  registry is unavailable (Lakebase is the sole structured-data
  backend since v0.4.0), so the probes report a warning.

Each probe returns ``{name, label, status, detail, duration_ms}``;
the top-level ``status`` is the worst severity across all probes.
``GET /health/detailed`` was removed — its information is now part of
``GET /health``.

The endpoint stays anonymous: ``/health`` is in the bypass list of
:class:`PermissionMiddleware`, :class:`CSRFMiddleware` and
:class:`RequestTimingMiddleware`, so external uptime probes (load
balancer, k8s liveness/readiness, Datadog) can call it without a
session cookie.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from back.core.helpers import run_blocking
from back.core.logging import get_logger
from shared.config.constants import APP_VERSION
from shared.config.RuntimeEnv import RuntimeEnv
from shared.config.settings import Settings, get_settings

logger = get_logger(__name__)

#: The built-in ``SECRET_KEY``. Read off the field default rather than restated,
#: so this check cannot drift from ``Settings`` if the default ever changes.
_DEFAULT_SECRET_KEY = Settings.model_fields["secret_key"].default

router = APIRouter(tags=["Health"])

_OK = "ok"
_WARNING = "warning"
_ERROR = "error"
_SEVERITY_RANK = {_OK: 0, _WARNING: 1, _ERROR: 2}


# ---------------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------------


def _safely_run(name: str, label: str, fn: Callable[[], tuple[str, str]]) -> dict[str, Any]:
    """Run *fn* and convert it to a stable check dict.

    *fn* is expected to return ``(status, detail)``. Any exception is
    caught and surfaced as ``error`` so a single broken probe never
    fails the whole readiness response.
    """
    started = time.monotonic()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 — catch-all is the point
        logger.exception("Health check %s raised: %s", name, exc)
        status, detail = _ERROR, f"Probe raised: {exc}"
    return {
        "name": name,
        "label": label,
        "status": status,
        "detail": detail,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# Filesystem probes
# ---------------------------------------------------------------------------


def _format_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.2f} GB"


def _check_directory_writable(path: str, *, low_warn_gb: float = 1.0, low_err_gb: float = 0.1) -> tuple[str, str]:
    """Generic "this directory is usable" probe.

    Verifies the directory exists (creating it if missing), is
    writable, and has enough free space. ``low_warn_gb`` / ``low_err_gb``
    define the warning / error thresholds in GiB.
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return _ERROR, f"Cannot create {path}: {exc}"

    if not os.access(path, os.W_OK):
        return _ERROR, f"{path} is not writable by the app process"

    sentinel = os.path.join(path, f".health_{uuid.uuid4().hex[:8]}")
    try:
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(sentinel)
    except OSError as exc:
        return _ERROR, f"Write probe failed at {path}: {exc}"

    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    base_msg = (
        f"Writable; {_format_gb(usage.free)} free of {_format_gb(usage.total)}"
    )
    if free_gb < low_err_gb:
        return _ERROR, f"Critically low disk space — {base_msg}"
    if free_gb < low_warn_gb:
        return _WARNING, f"Low disk space — {base_msg}"
    return _OK, base_msg


def _check_tmp() -> tuple[str, str]:
    return _check_directory_writable("/tmp", low_warn_gb=1.0, low_err_gb=0.1)


def _check_session_dir(settings: Settings) -> tuple[str, str]:
    return _check_directory_writable(
        settings.session_dir, low_warn_gb=0.5, low_err_gb=0.05
    )


def _check_log_dir() -> tuple[str, str]:
    """Resolve the live log directory and verify it is writable."""
    from back.core.logging.LogManager import LogManager

    mgr = LogManager.instance()
    log_path = mgr.log_path
    if not log_path:
        # Logging may not be configured yet (e.g. running under tests
        # that imported this module before ``LogManager.setup``). Treat
        # as advisory rather than failing the probe.
        return _WARNING, "Log manager has not been initialised yet"
    log_dir = os.path.dirname(log_path)
    return _check_directory_writable(log_dir, low_warn_gb=0.5, low_err_gb=0.05)


# ---------------------------------------------------------------------------
# Databricks probes
# ---------------------------------------------------------------------------


def _check_databricks_auth() -> tuple[str, str]:
    """Verify the app has usable Databricks credentials.

    Exercises the active auth path eagerly (M2M OAuth for a service
    principal, the Databricks SDK ``Config.authenticate`` call for CLI
    mode) so a misconfigured workspace fails here rather than at the first
    warehouse call.
    """
    from back.core.databricks.DatabricksAuth import DatabricksAuth

    #: Any of these being set means the operator *intended* workspace API
    #: access, so unusable credentials are a misconfiguration. None of them
    #: being set is a Databricks-free deployment, which is supported.
    #:
    #: ``DATABRICKS_HOST`` is deliberately **not** here. It is also the input
    #: :class:`OIDCClient` derives its login endpoints from, so a deployment
    #: using Databricks purely for SSO sets it and nothing else — a legitimate
    #: configuration that this check flagged as broken. These four are set for
    #: no reason other than API access.
    _INTENT_VARS = (
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CONFIG_PROFILE",
    )

    auth = DatabricksAuth()
    if not auth.has_valid_auth():
        configured = [v for v in _INTENT_VARS if os.getenv(v)]
        if not configured:
            # Databricks is an optional connector. Reporting _ERROR here made
            # /health report status="error" forever on a container + Postgres
            # deployment, which is exactly the shape the decoupling work made
            # first-class -- and it made the top-level `status` field useless
            # for monitoring, since it could never go green.
            return (
                _WARNING,
                "Databricks not configured (optional). Unity Catalog Volume "
                "documents, SQL warehouse ingestion and Foundation Model "
                "endpoints are unavailable; the registry, graph DB and "
                "reasoning run on PostgreSQL.",
            )
        return (
            _ERROR,
            "Databricks is partially configured ("
            + ", ".join(configured)
            + ") but credentials are not usable. Set DATABRICKS_CLIENT_ID + "
            "DATABRICKS_CLIENT_SECRET (service principal), or DATABRICKS_TOKEN, "
            "or configure a Databricks CLI profile in ~/.databrickscfg "
            "(run `databricks auth login`). Unset them all to run without "
            "Databricks.",
        )
    if auth.auth_mode == "app":
        try:
            auth.get_oauth_token()
        except Exception as exc:  # noqa: BLE001 — vendor surface
            return _ERROR, f"OAuth token request failed: {exc}"
        return _OK, f"Service-principal OAuth credentials valid (host={auth.host})"
    if auth.auth_mode == "cli":
        try:
            auth.get_bearer_token()
        except Exception as exc:  # noqa: BLE001 — vendor surface
            return _ERROR, f"Databricks CLI profile authentication failed: {exc}"
        return (
            _OK,
            f"Databricks CLI profile '{auth.cli_profile_name}' configured "
            f"(host={auth.host})",
        )
    return _OK, f"Personal Access Token configured (host={auth.host})"


def _build_health_client(settings: Settings | None = None):
    """Instantiate a ``DatabricksClient`` with no domain/session.

    ``get_databricks_client`` already supports a ``None`` domain via
    ``RegistryCfg.from_domain(None, settings)``-style fallbacks, so the
    readiness route does not need a SessionManager.
    """
    from back.core.helpers import get_databricks_client

    return get_databricks_client(None, settings or get_settings())


def _check_warehouse(settings: Settings | None = None) -> tuple[str, str]:
    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials available — warehouse not probed"
    if not getattr(client, "warehouse_id", ""):
        return _WARNING, "DATABRICKS_SQL_WAREHOUSE_ID is not configured"
    ok, msg = client.test_connection()
    return (_OK if ok else _ERROR), msg


def _check_cloud_fetch(settings: Settings | None = None) -> tuple[str, str]:
    """Report CloudFetch capability via the real runtime probe.

    Always calls :meth:`DatabricksAuth.probe_cloud_fetch_capability`,
    which issues a tiny ``SELECT 1`` with ``use_cloud_fetch=True`` and
    surfaces the actual outcome. Result is cached on the auth instance
    so SQL connections share the same verdict.
    """
    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "Databricks credentials unavailable — CloudFetch not probed"
    if not getattr(client, "warehouse_id", ""):
        return _WARNING, "SQL warehouse not configured — CloudFetch not probed"

    capable, reason = client.auth.probe_cloud_fetch_capability()
    if capable:
        return _OK, f"CloudFetch enabled — {reason}"
    return _WARNING, f"CloudFetch unavailable — {reason}"


# ---------------------------------------------------------------------------
# Registry probes
# ---------------------------------------------------------------------------


def _resolve_registry_cfg(settings: Settings):
    from back.objects.registry import RegistryCfg

    return RegistryCfg.from_domain(None, settings)


def _check_registry_cfg(settings: Settings) -> tuple[str, str]:
    cfg = _resolve_registry_cfg(settings)
    if not cfg.has_volume:
        # ``has_volume``, not ``is_configured``: this probe is about the
        # optional UC Volume for binary document uploads, not the registry
        # itself, which lives in Postgres and is probed separately.
        return (
            _WARNING,
            "No Unity Catalog Volume configured (optional) — document uploads "
            "are unavailable. Set REGISTRY_CATALOG / REGISTRY_SCHEMA / "
            "REGISTRY_VOLUME, or REGISTRY_VOLUME_PATH, to enable them. The "
            "registry itself is unaffected; see the 'postgres' check.",
        )
    return (
        _OK,
        f"catalog={cfg.catalog} schema={cfg.schema} volume={cfg.volume} "
        f"postgres_schema={cfg.postgres_schema}",
    )


def _check_registry_volume_read(settings: Settings) -> tuple[str, str]:
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema and cfg.volume):
        return _WARNING, "Registry volume not configured — skipped"

    from back.core.databricks.DatabricksAuth import DatabricksAuth
    from back.core.databricks.uc import VolumeFileService

    svc = VolumeFileService(auth=DatabricksAuth())
    if not svc.is_configured():
        return _ERROR, "Databricks credentials not available for Files API"
    vol_path = f"/Volumes/{cfg.catalog}/{cfg.schema}/{cfg.volume}"
    ok, items, msg = svc.list_directory(vol_path)
    if ok:
        return _OK, f"Listed {vol_path} — {len(items)} entries"
    return _ERROR, f"Cannot list {vol_path}: {msg}"


def _check_registry_volume_write(settings: Settings) -> tuple[str, str]:
    """End-to-end write probe — write a tiny sentinel and delete it.

    Far stronger than ``SHOW GRANTS`` because it actually exercises the
    same Files API code path that the registry uses to persist
    ``.global_config.json`` and binary archives.
    """
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema and cfg.volume):
        return _WARNING, "Registry volume not configured — skipped"

    from back.core.databricks.DatabricksAuth import DatabricksAuth
    from back.core.databricks.uc import VolumeFileService

    svc = VolumeFileService(auth=DatabricksAuth())
    if not svc.is_configured():
        return _ERROR, "Databricks credentials not available for Files API"

    sentinel = (
        f"/Volumes/{cfg.catalog}/{cfg.schema}/{cfg.volume}"
        f"/.health_check_{uuid.uuid4().hex[:8]}.txt"
    )
    ok, msg = svc.write_file(sentinel, "ok")
    if not ok:
        return _ERROR, f"Volume write failed ({sentinel}): {msg}"
    # Best-effort cleanup; a leftover file is harmless but noisy.
    deleted, _del_msg = svc.delete_file(sentinel)
    if deleted:
        return _OK, f"Wrote+deleted sentinel at {sentinel}"
    return (
        _WARNING,
        f"Wrote sentinel but cleanup failed (please remove manually): {sentinel}",
    )


def _check_registry_uc_schema_ddl() -> tuple[str, str]:
    """Probe ``CREATE OR REPLACE VIEW`` in the registry schema.

    The Digital-Twin build creates views in the registry catalog/schema.
    Failing this probe at startup catches missing
    ``CREATE`` / ``USE_SCHEMA`` grants long before the build job
    surfaces an opaque ``PERMISSION_DENIED`` deep in a SQL stack.
    """
    settings = get_settings()
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema):
        return _WARNING, "Registry catalog/schema not configured — skipped"

    client = _build_health_client()
    if client is None:
        return _WARNING, "No Databricks credentials — DDL probe skipped"
    if not getattr(client, "warehouse_id", ""):
        return _WARNING, "No SQL warehouse configured — DDL probe skipped"

    name = f"_ontobricks_health_{uuid.uuid4().hex[:8]}"
    fqn = f"`{cfg.catalog}`.`{cfg.schema}`.`{name}`"
    try:
        client.execute_statement(f"CREATE OR REPLACE VIEW {fqn} AS SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        return _ERROR, f"Cannot create view in {cfg.catalog}.{cfg.schema}: {exc}"
    try:
        client.execute_statement(f"DROP VIEW IF EXISTS {fqn}")
    except Exception as exc:  # noqa: BLE001
        # Created but couldn't clean up — admins will see the stray view.
        return _WARNING, f"View created but DROP failed for {fqn}: {exc}"
    return _OK, f"CREATE/DROP VIEW succeeded in {cfg.catalog}.{cfg.schema}"


# ---------------------------------------------------------------------------
# Graph DB (Lakebase graph schema) probe
# ---------------------------------------------------------------------------


def _check_graphdb_postgres(settings: Settings) -> tuple[str, str]:
    """Probe the configured Graph DB Lakebase database and graph schema.

    Uses the same auth selection as :class:`GraphDBFactory._create_lakebase`:
    A branch override from ``graph_engine_config.lakebase_branch`` when set,
    otherwise the bound Lakebase auth.  This ensures the health probe always
    targets the same host as the actual build engine — the graph DB may be
    on a completely different Lakebase project than the registry.
    """
    from back.core.databricks.lakebase import get_graph_auth

    cfg = _resolve_registry_cfg(settings)
    try:
        from back.objects.registry.store import RegistryFactory

        store = RegistryFactory.from_cfg(cfg)
        global_cfg = store.load_global_config()
        from back.core.graphdb.engine_config import postgres_section

        engine_cfg = postgres_section(global_cfg.get("graph_engine_config") or {})
    except Exception as exc:  # noqa: BLE001
        return _WARNING, f"Could not load graph engine config: {exc}"

    database = (engine_cfg.get("database") or "").strip()
    schema = (engine_cfg.get("schema") or engine_cfg.get("graph_schema") or "ontobricks_graph").strip()
    branch_path = (engine_cfg.get("lakebase_branch") or "").strip()

    if not schema:
        return _WARNING, "Graph DB schema not configured — set it in Settings → Graph DB"

    auth = get_graph_auth(branch_path, database)

    if not auth.is_available:
        return (
            _WARNING,
            "Lakebase not bound (PG* env vars unset) — Graph DB not probed",
        )

    try:
        from back.core.graphdb.postgres.pool import _require_psycopg

        psycopg, _ = _require_psycopg()
        kwargs = auth.kwargs(application_name="ontobricks-graphdb-health")
        if database:
            kwargs["dbname"] = database

        with psycopg.connect(**kwargs) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            row = cur.fetchone() or ("?", "?")
            cur_db, cur_user = row[0], row[1]
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = %s)",
                (schema,),
            )
            schema_exists = bool((cur.fetchone() or [False])[0])
        if schema_exists:
            return _OK, f"Graph DB reachable — db={cur_db} schema={schema} user={cur_user}"
        return (
            _WARNING,
            f"Graph DB connected (db={cur_db}) but schema '{schema}' does not exist yet — "
            "run a Knowledge Graph build to create it",
        )
    except Exception as exc:  # noqa: BLE001
        return _ERROR, f"Graph DB probe failed (database={database or 'default'}, schema={schema}): {exc}"


# ---------------------------------------------------------------------------
# Lakebase probe
# ---------------------------------------------------------------------------


def _check_postgres(settings: Settings) -> tuple[str, str]:
    from back.core.databricks.lakebase import get_lakebase_auth

    auth = get_lakebase_auth()
    if not auth.is_available:
        return (
            _WARNING,
            "Lakebase not bound (PG* env vars unset) — registry is unavailable; "
            "set PGHOST + PGUSER + PGDATABASE in .env, or bind a database "
            "resource (deployed)",
        )

    cfg = _resolve_registry_cfg(settings)
    from back.objects.registry.store.postgres.store import PostgresRegistryStore

    store = PostgresRegistryStore(
        registry_cfg=cfg,
        schema=cfg.postgres_schema or "ontobricks_registry",
        database=cfg.postgres_database or "",
    )
    status_dict = store.init_status()
    reason = status_dict.get("reason", "unknown")
    err = status_dict.get("error") or status_dict.get("reason")
    if status_dict.get("initialized"):
        return _OK, f"Lakebase ready — schema={store.schema} ({reason})"
    if reason in ("no_registries_table", "no_registry_row"):
        # Schema reachable, just not bootstrapped — admins can run
        # *Initialize* from Settings → Registry. Treat as warning.
        return _WARNING, str(err)
    # ``no_usage`` / ``connect_failed`` / unknown — these block the app.
    return _ERROR, str(err)


def _check_postgres_permissions(settings: Settings) -> tuple[str, str]:
    """Verify Lakebase registry privileges expected by OntoBricks runtime."""
    from back.core.databricks.lakebase import get_lakebase_auth

    auth = get_lakebase_auth()
    if not auth.is_available:
        return (
            _WARNING,
            "Lakebase not bound (PG* env vars unset) — permission checks skipped",
        )

    cfg = _resolve_registry_cfg(settings)
    from back.objects.registry.store.postgres.store import PostgresRegistryStore

    store = PostgresRegistryStore(
        registry_cfg=cfg,
        schema=cfg.postgres_schema or "ontobricks_registry",
        database=cfg.postgres_database or "",
    )
    status_dict = store.init_status()
    reason = status_dict.get("reason", "unknown")
    err = status_dict.get("error") or status_dict.get("reason")
    if reason == "no_usage":
        return _ERROR, str(err)
    if reason in ("no_registries_table", "no_registry_row"):
        return (
            _WARNING,
            f"Registry not initialized ({reason}) — permission probe partial: {err}",
        )
    if reason != "ok":
        return _ERROR, f"Lakebase probe unavailable ({reason}): {err}"

    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_user, "
                "       has_schema_privilege(current_user, %s, 'USAGE'), "
                "       has_schema_privilege(current_user, %s, 'CREATE')",
                (store.schema, store.schema),
            )
            row = cur.fetchone() or ("?", "?", False, False)
            cur_db, cur_user, has_usage, has_create = row

            cur.execute(
                "SELECT "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'SELECT')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'INSERT')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'UPDATE')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'DELETE')), true), "
                "COUNT(*) "
                "FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type='BASE TABLE'",
                (store.schema,),
            )
            tbl_sel, tbl_ins, tbl_upd, tbl_del, tbl_count = cur.fetchone() or (
                True,
                True,
                True,
                True,
                0,
            )

            cur.execute(
                "SELECT "
                "COALESCE(bool_and(has_sequence_privilege("
                "current_user, format('%%I.%%I', sequence_schema, sequence_name), 'USAGE')), true), "
                "COALESCE(bool_and(has_sequence_privilege("
                "current_user, format('%%I.%%I', sequence_schema, sequence_name), 'SELECT')), true), "
                "COALESCE(bool_and(has_sequence_privilege("
                "current_user, format('%%I.%%I', sequence_schema, sequence_name), 'UPDATE')), true), "
                "COUNT(*) "
                "FROM information_schema.sequences "
                "WHERE sequence_schema = %s",
                (store.schema,),
            )
            seq_use, seq_sel, seq_upd, seq_count = cur.fetchone() or (True, True, True, 0)
    except Exception as exc:  # noqa: BLE001
        return _ERROR, f"Lakebase permission probe failed: {exc}"

    missing: list[str] = []
    if not has_usage:
        missing.append("schema USAGE")
    if not has_create:
        missing.append("schema CREATE")
    if not tbl_sel:
        missing.append("table SELECT")
    if not tbl_ins:
        missing.append("table INSERT")
    if not tbl_upd:
        missing.append("table UPDATE")
    if not tbl_del:
        missing.append("table DELETE")
    if not seq_use:
        missing.append("sequence USAGE")
    if not seq_sel:
        missing.append("sequence SELECT")
    if not seq_upd:
        missing.append("sequence UPDATE")

    if missing:
        return (
            _ERROR,
            "Missing Lakebase grants for role "
            f"'{cur_user}' on {cur_db}.{store.schema}: {', '.join(missing)}. "
            "Grant it: GRANT USAGE, CREATE ON SCHEMA <schema> TO <role>.",
        )

    return (
        _OK,
        f"Lakebase permissions OK ({cur_db}.{store.schema}; "
        f"tables={int(tbl_count)}, sequences={int(seq_count)})",
    )


# ---------------------------------------------------------------------------
# Lakebase Accelerated Sync probe
# ---------------------------------------------------------------------------




def _check_uc_catalog_privileges(settings: Settings) -> tuple[str, str]:
    """Verify the app identity can USE the registry UC catalog (list its schemas)."""
    cfg = _resolve_registry_cfg(settings)
    if not cfg.catalog:
        return (
            _WARNING,
            "Registry catalog not configured — set REGISTRY_VOLUME_PATH or bind a UC Volume "
            "resource",
        )
    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials — skipped"
    try:
        schemas = client.get_schemas(cfg.catalog)
        if isinstance(schemas, list):
            return (
                _OK,
                f"USE CATALOG granted on '{cfg.catalog}' — {len(schemas)} schema(s) visible. "
                "The identity OntoBricks connects as can list and access schemas "
                    "within this catalog.",
            )
        return _WARNING, "Could not determine catalog access — unexpected response from UC API"
    except Exception as exc:
        err = str(exc)
        if any(k in err.upper() for k in ("PERMISSION_DENIED", "CATALOG_NOT_FOUND", "UNAUTHORIZED")):
            return (
                _ERROR,
                f"Cannot USE catalog '{cfg.catalog}': {err}. "
                "Grant USE CATALOG on this catalog to the identity OntoBricks "
                "connects as (DATABRICKS_CLIENT_ID, or the signed-in user) via: "
                f"GRANT USE CATALOG ON CATALOG `{cfg.catalog}` TO `<principal>`",
            )
        return _ERROR, f"Catalog privilege check failed: {exc}"


def _check_uc_schema_privileges(settings: Settings) -> tuple[str, str]:
    """Verify USE SCHEMA on the registry schema (list its objects)."""
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema):
        return _WARNING, "Registry catalog/schema not configured"
    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials — skipped"
    try:
        objects = client.list_tables_and_views(cfg.catalog, cfg.schema)
        if isinstance(objects, list):
            return (
                _OK,
                f"USE SCHEMA granted on '{cfg.catalog}.{cfg.schema}' — "
                f"{len(objects)} object(s) visible. "
                "The app can list tables and views in this schema.",
            )
        return _WARNING, "Could not determine schema access — unexpected response from UC API"
    except Exception as exc:
        err = str(exc)
        if any(k in err.upper() for k in ("PERMISSION_DENIED", "SCHEMA_NOT_FOUND", "UNAUTHORIZED")):
            return (
                _ERROR,
                f"Cannot USE schema '{cfg.schema}': {err}. "
                f"Grant: USE SCHEMA ON SCHEMA `{cfg.catalog}`.`{cfg.schema}` TO `<app-sp>`",
            )
        return _ERROR, f"Schema privilege check failed: {exc}"


def _check_uc_create_table_privilege(settings: Settings) -> tuple[str, str]:
    """Probe CREATE TABLE + DROP TABLE in the registry schema.

    The Delta triple-store backend creates Delta TABLEs (not just VIEWs) in the
    registry schema during Knowledge Graph builds.  The existing ``_check_registry_uc_schema_ddl``
    probe only tests CREATE VIEW — a principal may have CREATE on views but not tables.
    This probe catches that gap before the first build attempts it.
    """
    settings = settings or get_settings()
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema):
        return _WARNING, "Registry catalog/schema not configured — skipped"

    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials — skipped"
    if not getattr(client, "warehouse_id", ""):
        return _WARNING, "No SQL warehouse configured — CREATE TABLE probe skipped"

    name = f"_ontobricks_health_{uuid.uuid4().hex[:8]}"
    fqn = f"`{cfg.catalog}`.`{cfg.schema}`.`{name}`"
    try:
        client.execute_statement(
            f"CREATE TABLE IF NOT EXISTS {fqn} (id BIGINT) USING DELTA"
        )
    except Exception as exc:
        return (
            _ERROR,
            f"Cannot CREATE TABLE in {cfg.catalog}.{cfg.schema}: {exc}. "
            "Required for Delta triple-store builds. "
            f"Grant: CREATE ON SCHEMA `{cfg.catalog}`.`{cfg.schema}` TO `<app-sp>`",
        )
    try:
        client.execute_statement(f"DROP TABLE IF EXISTS {fqn}")
    except Exception as exc:
        return (
            _WARNING,
            f"Table created but DROP failed for {fqn}: {exc}. "
            "Please drop it manually.",
        )
    return _OK, f"CREATE TABLE / DROP TABLE succeeded in {cfg.catalog}.{cfg.schema}"


def _check_lakebase_env_vars() -> tuple[str, str]:
    """Verify the Lakebase PG* env vars are present (must be injected by Databricks Apps).

    OntoBricks requires ``PGHOST``, ``PGDATABASE``, and ``PGUSER`` to be set.
    ``PGPORT`` defaults to 5432 when absent.  The Postgres password is not an
    env var — it is a short-lived JWT minted by :class:`LakebaseAuth` on demand.

    In a Databricks App these are injected automatically when a ``database``
    ``database`` resource is bound. Otherwise they must be set
    in ``.env``
    can be used instead of raw ``PG*`` values — see ``LakebaseAuth`` docs).
    """
    missing: list[str] = []
    present: list[str] = []

    for var in ("PGHOST", "PGDATABASE", "PGUSER"):
        if os.environ.get(var, "").strip():
            present.append(var)
        else:
            missing.append(var)

    port = os.environ.get("PGPORT", "5432")

    if missing:
        pghost_present = "PGHOST" in present
        return (
            _ERROR,
            f"Required Postgres env vars missing: {', '.join(missing)}. "
            f"Present: {', '.join(present) or 'none'}. "
            "In a deployed Databricks App these are injected from the ``database`` "
            "resource binding. Otherwise set PGHOST, PGDATABASE and PGUSER — "
            "Lakebase is reached the same way as any other PostgreSQL server.",
        )
    pghost = os.environ.get("PGHOST", "")
    pgdb   = os.environ.get("PGDATABASE", "")
    pguser = os.environ.get("PGUSER", "")
    return (
        _OK,
        f"PGHOST={pghost} PGDATABASE={pgdb} PGUSER={pguser} PGPORT={port}. "
        "All required Lakebase env vars are present.",
    )


def _check_lakebase_psycopg() -> tuple[str, str]:
    """Verify the ``psycopg`` (v3) driver is installed and importable.

    ``psycopg`` is the only Postgres client used by OntoBricks; it is listed in
    ``pyproject.toml`` under ``[project.dependencies]`` as ``psycopg[binary]``.
    A missing or broken install would produce an opaque ``ImportError`` buried
    inside a connection attempt rather than a clear error.
    """
    try:
        import psycopg as _psycopg  # noqa: F401

        ver = getattr(_psycopg, "__version__", "?")
        return _OK, f"psycopg {ver} is installed and importable."
    except ImportError as exc:
        return (
            _ERROR,
            f"psycopg is not importable: {exc}. "
            "Install it with: pip install 'psycopg[binary]'  "
            "(or 'psycopg[c]' for the C extension build). "
            "Check pyproject.toml [project.dependencies].",
        )


def _check_lakebase_registry_initialized(settings: Settings) -> tuple[str, str]:
    """Verify the registry schema has been initialized (registries row exists).

    The app cannot function without at least one row in the ``registries`` table
    that points to the correct catalog/schema/volume. This row is created by
    Settings → Registry → Initialize (or the ``initialize_registry`` API call).
    """
    from back.core.databricks.lakebase import get_lakebase_auth

    auth = get_lakebase_auth()
    if not auth.is_available:
        return _WARNING, "Lakebase not bound — skipped"

    cfg = _resolve_registry_cfg(settings)
    from back.objects.registry.store.postgres.store import PostgresRegistryStore

    store = PostgresRegistryStore(
        registry_cfg=cfg,
        schema=cfg.postgres_schema or "ontobricks_registry",
        database=cfg.postgres_database or "",
    )
    status_dict = store.init_status()
    reason = status_dict.get("reason", "unknown")
    err = status_dict.get("error", "")

    if reason == "ok":
        try:
            with store._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT catalog, schema, volume FROM "{store.schema}".registries '
                    "ORDER BY created_at ASC LIMIT 1"
                )
                row = cur.fetchone()
            if row:
                return (
                    _OK,
                    f"Registry row found — catalog={row[0]} schema={row[1]} volume={row[2]}. "
                    "The registry is initialized and the catalog/schema/volume triplet "
                    "is correctly stored in Lakebase.",
                )
            return (
                _WARNING,
                f"Registry schema '{store.schema}' exists but the registries table is empty. "
                "Run Settings → Registry → Initialize to create the registry row.",
            )
        except Exception as exc:
            return _WARNING, f"Could not read registry row: {exc}"

    if reason == "no_registry_row":
        return (
            _ERROR,
            f"The registries table exists in schema '{store.schema}' but has no row. "
            "Run Settings → Registry → Initialize to create the registry entry. "
            "Without this, the app cannot resolve domain paths or load global config.",
        )
    if reason == "no_registries_table":
        return (
            _ERROR,
            f"The 'registries' table is missing from schema '{store.schema}'. "
            "Run Settings → Registry → Initialize to create all registry tables.",
        )
    if reason == "no_usage":
        return (
            _ERROR,
            err or f"Role lacks USAGE on schema '{store.schema}'.",
        )
    return _ERROR, f"Registry not initialized ({reason}): {err}"


def _check_lakebase_registry_tables(settings: Settings) -> tuple[str, str]:
    """Verify all expected Lakebase registry tables exist in the registry schema.

    Tables are split into three tiers:

    * **core** — blocking: the app cannot operate without these.
    * **optional** — created on first use; warning if missing.
    * **lazy** — created by the first relevant operation (e.g.
      ``domain_change_events`` is created by the first domain save that
      flushes the change-audit buffer).  These are expected to be absent
      on a fresh install and are reported informatively only.
    """
    from back.core.databricks.lakebase import get_lakebase_auth

    auth = get_lakebase_auth()
    if not auth.is_available:
        return _WARNING, "Lakebase not bound (PG* env vars unset) — skipped"

    cfg = _resolve_registry_cfg(settings)
    from back.objects.registry.store.postgres.store import (
        _KNOWN_TABLES,
        PostgresRegistryStore,
    )

    store = PostgresRegistryStore(
        registry_cfg=cfg,
        schema=cfg.postgres_schema or "ontobricks_registry",
        database=cfg.postgres_database or "",
    )
    _CORE_TABLES = frozenset(
        {
            "registries",
            "global_config",
            "domains",
            "domain_versions",
            "domain_permissions",
            "schedules",
            "schedule_runs",
            "build_runs",
        }
    )
    # domain_change_events is created lazily on the first domain save that
    # flushes the change-audit buffer; it is intentionally absent from
    # _KNOWN_TABLES so row-count probes skip it.  We note it separately.
    _LAZY_TABLES = frozenset({"domain_change_events"})
    # Expected sequences: one per bigserial primary key
    _EXPECTED_SEQUENCES = frozenset(
        {"schedule_runs_id_seq", "build_runs_id_seq", "graph_analytics_runs_id_seq"}
    )

    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (store.schema,),
            )
            existing_tables = {row[0] for row in cur.fetchall()}

            cur.execute(
                "SELECT sequence_name FROM information_schema.sequences "
                "WHERE sequence_schema = %s",
                (store.schema,),
            )
            existing_seqs = {row[0] for row in cur.fetchall()}

        missing_core  = _CORE_TABLES - existing_tables
        missing_opt   = _KNOWN_TABLES - existing_tables - _CORE_TABLES
        lazy_present  = _LAZY_TABLES & existing_tables
        missing_seqs  = _EXPECTED_SEQUENCES - existing_seqs

        if missing_core:
            return (
                _ERROR,
                f"Missing core registry tables in schema '{store.schema}': "
                f"{', '.join(sorted(missing_core))}. "
                "Run Settings → Registry → Initialize to bootstrap the schema.",
            )

        present   = existing_tables & _KNOWN_TABLES
        seq_note  = (
            f"; sequences: {len(existing_seqs - missing_seqs)}/{len(_EXPECTED_SEQUENCES)} OK"
            if missing_seqs
            else f"; {len(existing_seqs)} sequence(s) present"
        )
        lazy_note = "; lazy table 'domain_change_events' present" if lazy_present else ""

        base_msg = (
            f"{len(present)}/{len(_KNOWN_TABLES)} schema-DDL tables present in "
            f"'{store.schema}'{seq_note}{lazy_note}"
        )

        if missing_seqs:
            return (
                _WARNING,
                base_msg
                + f". Missing sequences: {', '.join(sorted(missing_seqs))} — "
                "these are created by schema initialization; run "
                "Settings → Registry → Initialize.",
            )
        if missing_opt:
            return (
                _WARNING,
                base_msg
                + ". Optional tables absent (created on first use): "
                + ", ".join(sorted(missing_opt)),
            )
        return _OK, base_msg
    except Exception as exc:
        return _ERROR, f"Registry table existence check failed: {exc}"


def _check_graphdb_tables(settings: Settings) -> tuple[str, str]:
    """Report how many tables / views are in the configured graph DB schema."""
    from back.core.databricks.lakebase import get_graph_auth

    cfg = _resolve_registry_cfg(settings)
    try:
        from back.objects.registry.store import RegistryFactory

        store = RegistryFactory.from_cfg(cfg)
        global_cfg = store.load_global_config()
        from back.core.graphdb.engine_config import postgres_section

        engine_cfg = postgres_section(global_cfg.get("graph_engine_config") or {})
    except Exception as exc:
        return _WARNING, f"Could not load graph engine config: {exc}"

    database = (engine_cfg.get("database") or "").strip()
    schema = (
        engine_cfg.get("schema") or engine_cfg.get("graph_schema") or "ontobricks_graph"
    ).strip()
    branch_path = (engine_cfg.get("lakebase_branch") or "").strip()

    auth = get_graph_auth(branch_path, database)
    if not auth.is_available:
        return _WARNING, "Lakebase not bound — Graph DB not probed"

    try:
        from back.core.graphdb.postgres.pool import _require_psycopg

        psycopg, _ = _require_psycopg()
        kwargs = auth.kwargs(application_name="ontobricks-graphdb-diag")
        if database:
            kwargs["dbname"] = database

        with psycopg.connect(**kwargs) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            table_count = int((cur.fetchone() or [0])[0])
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.views WHERE table_schema = %s",
                (schema,),
            )
            view_count = int((cur.fetchone() or [0])[0])

        if table_count == 0 and view_count == 0:
            return (
                _WARNING,
                f"Graph schema '{schema}' is empty — no tables or views yet. "
                "Run Knowledge Graph → Build to populate it (one table + one VIEW per "
                "domain version, e.g. <domain>_<version> + <domain>_<version>_data).",
            )
        return (
            _OK,
            f"Graph schema '{schema}': {table_count} table(s), {view_count} view(s). "
            "Each Knowledge Graph build creates a raw triple table and a materialized _data table.",
        )
    except Exception as exc:
        return _ERROR, f"Graph DB table check failed (schema={schema}): {exc}"


def _check_graphdb_permissions(settings: Settings) -> tuple[str, str]:
    """Verify SELECT / INSERT / UPDATE / DELETE on the graph DB schema tables."""
    from back.core.databricks.lakebase import get_graph_auth

    cfg = _resolve_registry_cfg(settings)
    try:
        from back.objects.registry.store import RegistryFactory

        store = RegistryFactory.from_cfg(cfg)
        global_cfg = store.load_global_config()
        from back.core.graphdb.engine_config import postgres_section

        engine_cfg = postgres_section(global_cfg.get("graph_engine_config") or {})
    except Exception as exc:
        return _WARNING, f"Could not load graph engine config: {exc}"

    database = (engine_cfg.get("database") or "").strip()
    schema = (
        engine_cfg.get("schema") or engine_cfg.get("graph_schema") or "ontobricks_graph"
    ).strip()
    branch_path = (engine_cfg.get("lakebase_branch") or "").strip()

    auth = get_graph_auth(branch_path, database)
    if not auth.is_available:
        return _WARNING, "Lakebase not bound — Graph DB permissions not probed"

    try:
        from back.core.graphdb.postgres.pool import _require_psycopg

        psycopg, _ = _require_psycopg()
        kwargs = auth.kwargs(application_name="ontobricks-graphdb-diag")
        if database:
            kwargs["dbname"] = database

        with psycopg.connect(**kwargs) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_schema_privilege(current_user, %s, 'USAGE'), "
                "       has_schema_privilege(current_user, %s, 'CREATE'), "
                "       current_user",
                (schema, schema),
            )
            row = cur.fetchone() or (False, False, "?")
            has_usage, has_create, cur_user = row

            cur.execute(
                "SELECT "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'SELECT')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'INSERT')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'UPDATE')), true), "
                "COALESCE(bool_and(has_table_privilege("
                "current_user, format('%%I.%%I', table_schema, table_name), 'DELETE')), true), "
                "COUNT(*) "
                "FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (schema,),
            )
            tbl_sel, tbl_ins, tbl_upd, tbl_del, tbl_count = cur.fetchone() or (
                True,
                True,
                True,
                True,
                0,
            )

        missing: list[str] = []
        if not has_usage:
            missing.append("schema USAGE")
        if not has_create:
            missing.append("schema CREATE (required to create triple tables per KG build)")
        if not tbl_sel:
            missing.append("table SELECT")
        if not tbl_ins:
            missing.append("table INSERT")
        if not tbl_upd:
            missing.append("table UPDATE")
        if not tbl_del:
            missing.append("table DELETE")

        if missing:
            return (
                _ERROR,
                f"Missing Postgres permissions on graph schema '{schema}' for role '{cur_user}': "
                f"{', '.join(missing)}. "
                "Run Settings → Lakebase → Permissions to grant superuser, or use "
                "GRANT USAGE, CREATE ON SCHEMA <schema> TO <role>.",
            )
        return (
            _OK,
            f"Graph DB permissions OK — role '{cur_user}' on schema '{schema}' "
            f"({int(tbl_count)} table(s)); USAGE + CREATE + SELECT/INSERT/UPDATE/DELETE all granted.",
        )
    except Exception as exc:
        return _ERROR, f"Graph DB permission check failed (schema={schema}): {exc}"




def _check_delta_warehouse(settings: Settings) -> tuple[str, str]:
    """Check whether the Lakehouse SQL warehouse is configured and reachable."""
    cfg = _resolve_registry_cfg(settings)
    try:
        from back.core.graphdb.engine_config import resolve_lakehouse_warehouse_id
        from back.objects.registry.store import RegistryFactory

        store = RegistryFactory.from_cfg(cfg)
        global_cfg = store.load_global_config()
        backend = global_cfg.get("triple_store_backend", "postgres")
        delta_warehouse_id = resolve_lakehouse_warehouse_id(
            global_cfg.get("graph_engine_config") or {}
        )
    except Exception as exc:
        return _WARNING, f"Could not read Lakehouse warehouse config: {exc}"

    if backend != "databricks":
        return (
            _OK,
            f"Delta backend not selected (current backend: {backend}) — skipped. "
            "Switch to Lakehouse in Settings → Back end → Lakehouse to enable.",
        )
    if not delta_warehouse_id:
        return (
            _WARNING,
            "Delta backend is selected but no dedicated Delta warehouse is configured. "
            "Set one in Settings → Lakehouse → SQL Warehouse (falls back to the global warehouse).",
        )

    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials — warehouse probe skipped"
    ok, msg = client.sql.test_connection()
    return (_OK if ok else _ERROR), f"Delta warehouse {delta_warehouse_id}: {msg}"


def _check_delta_objects_exist(settings: Settings) -> tuple[str, str]:
    """Check whether Delta triple-store objects exist in the registry UC schema."""
    cfg = _resolve_registry_cfg(settings)
    if not (cfg.catalog and cfg.schema):
        return _WARNING, "Registry catalog/schema not configured — skipped"

    try:
        from back.objects.registry.store import RegistryFactory

        store = RegistryFactory.from_cfg(cfg)
        global_cfg = store.load_global_config()
        backend = global_cfg.get("triple_store_backend", "postgres")
    except Exception as exc:
        return _WARNING, f"Could not read triple-store config: {exc}"

    if backend != "databricks":
        return (
            _OK,
            f"Delta backend not selected (current: {backend}) — skipped. "
            "Delta objects only exist after a Knowledge Graph build with the Delta backend.",
        )

    client = _build_health_client(settings)
    if client is None:
        return _WARNING, "No Databricks credentials — skipped"
    if not getattr(client, "warehouse_id", ""):
        return _WARNING, "No SQL warehouse configured — Delta objects check skipped"

    try:
        objects = client.list_tables_and_views(cfg.catalog, cfg.schema) or []
        if not objects:
            return (
                _WARNING,
                f"No objects found in {cfg.catalog}.{cfg.schema}. "
                "Run Knowledge Graph → Build to create the R2RML VIEW and _data Delta table "
                "for each domain version.",
            )
        data_tables = [o for o in objects if isinstance(o, dict) and o.get("name", "").endswith("_data")]
        views = [o for o in objects if isinstance(o, dict) and o.get("table_type") == "VIEW"]
        return (
            _OK,
            f"{len(objects)} object(s) in {cfg.catalog}.{cfg.schema}: "
            f"{len(views)} VIEW(s), {len(data_tables)} _data table(s).",
        )
    except Exception as exc:
        return _ERROR, f"Delta objects check failed: {exc}"


# ---------------------------------------------------------------------------
# Diagnostics aggregator (grouped by subsystem)
# ---------------------------------------------------------------------------


def run_diagnostics_checks(settings: Settings | None = None) -> dict[str, Any]:
    """Run comprehensive diagnostics, grouped by subsystem.

    Returns four groups:

    * **Unity Catalog — Registry** — catalog/schema/volume access + DDL privileges
    * **Lakebase — Registry** — Postgres connection, schema tables, permissions
    * **Lakebase — Graph DB** — graph schema connectivity, tables, permissions
    * **Delta Triple Store** — Delta warehouse, UC objects, Accelerated Sync

    The function is synchronous; wrap in :func:`run_blocking` from an ``async`` route.
    """
    settings = settings or get_settings()
    groups = []
    all_checks: list[dict[str, Any]] = []

    # ── Group 1: Unity Catalog — Registry ──────────────────────────────────
    uc_checks = [
        _safely_run(
            "uc.config",
            "Registry configuration resolved",
            lambda: _check_registry_cfg(settings),
        ),
        _safely_run(
            "uc.catalog",
            "USE CATALOG privilege",
            lambda: _check_uc_catalog_privileges(settings),
        ),
        _safely_run(
            "uc.schema",
            "USE SCHEMA privilege",
            lambda: _check_uc_schema_privileges(settings),
        ),
        _safely_run(
            "uc.view_ddl",
            "CREATE VIEW in schema (DDL probe)",
            _check_registry_uc_schema_ddl,
        ),
        _safely_run(
            "uc.table_ddl",
            "CREATE TABLE in schema (Delta build probe)",
            lambda: _check_uc_create_table_privilege(settings),
        ),
        _safely_run(
            "uc.volume_read",
            "UC Volume — list (READ VOLUME)",
            lambda: _check_registry_volume_read(settings),
        ),
        _safely_run(
            "uc.volume_write",
            "UC Volume — write sentinel (WRITE VOLUME)",
            lambda: _check_registry_volume_write(settings),
        ),
    ]
    groups.append(
        {
            "id": "uc_registry",
            "title": "Unity Catalog — Registry",
            "description": (
                "Verifies the privileges OntoBricks' Databricks identity needs on the Unity "
                "Catalog "
                "registry catalog and schema. "
                "Required grants: USE CATALOG (to navigate the catalog), "
                "USE SCHEMA (to list objects in the registry schema), "
                "CREATE (to materialise R2RML VIEWs and Delta tables during Knowledge Graph builds — "
                "tested separately for VIEWs and TABLEs since a grant may allow one but not the other), "
                "READ VOLUME + WRITE VOLUME (to store .obx exports, document uploads, "
                "and the global config blob on the UC Volume). "
                "Missing any of these grants will cause builds or registry saves to fail."
            ),
            "checks": uc_checks,
        }
    )
    all_checks.extend(uc_checks)

    # ── Group 2: Lakebase — Registry (Postgres) ────────────────────────────
    lb_checks = [
        _safely_run(
            "lakebase.psycopg",
            "psycopg driver installed",
            _check_lakebase_psycopg,
        ),
        _safely_run(
            "lakebase.env_vars",
            "Lakebase PG* env vars present",
            _check_lakebase_env_vars,
        ),
        _safely_run(
            "lakebase.connection",
            "Registry Postgres — connection + USAGE check",
            lambda: _check_postgres(settings),
        ),
        _safely_run(
            "lakebase.initialized",
            "Registry row exists (initialized)",
            lambda: _check_lakebase_registry_initialized(settings),
        ),
        _safely_run(
            "lakebase.tables",
            "Registry tables + sequences — existence",
            lambda: _check_lakebase_registry_tables(settings),
        ),
        _safely_run(
            "postgres.permissions",
            "Registry schema — Postgres DML privileges",
            lambda: _check_postgres_permissions(settings),
        ),
    ]
    groups.append(
        {
            "id": "lakebase_registry",
            "title": "Lakebase — Registry (Postgres)",
            "description": (
                "Checks the Lakebase Postgres instance used as the OntoBricks registry back-end. "
                "Pre-requisites in order: "
                "(1) psycopg v3 driver installed; "
                "(2) PGHOST + PGDATABASE + PGUSER env vars injected by Databricks Apps "
                "(or set manually in .env); "
                "(3) Postgres connection succeeds and the role has USAGE on the registry schema; "
                "(4) A registry row exists in the 'registries' table (created by Initialize); "
                "(5) All 14 schema-DDL tables + 3 sequences are present — "
                "core tables: registries, global_config, domains, domain_versions, "
                "domain_permissions, schedules, schedule_runs, build_runs; "
                "optional tables: graph_analytics, graph_analytics_runs, domain_review_events, "
                "domain_comments, domain_tasks, domain_edit_locks; "
                "lazy table: domain_change_events (created on first domain save); "
                "(6) The role has USAGE/CREATE on the schema + SELECT/INSERT/UPDATE/DELETE "
                "on all tables + USAGE/SELECT/UPDATE on all sequences. "
                "Run Settings → Registry → Initialize, or grant CREATE on the schema "
                "to fix permission and initialization issues."
            ),
            "checks": lb_checks,
        }
    )
    all_checks.extend(lb_checks)

    # ── Group 3: Lakebase — Graph DB ───────────────────────────────────────
    gdb_checks = [
        _safely_run(
            "graphdb.connection",
            "Graph DB — connection + schema exists",
            lambda: _check_graphdb_postgres(settings),
        ),
        _safely_run(
            "graphdb.tables",
            "Graph DB — schema tables & views",
            lambda: _check_graphdb_tables(settings),
        ),
        _safely_run(
            "graphdb.permissions",
            "Graph DB — Postgres USAGE + CREATE + DML on schema",
            lambda: _check_graphdb_permissions(settings),
        ),
    ]
    groups.append(
        {
            "id": "graphdb",
            "title": "Lakebase — Graph DB",
            "description": (
                "Checks the Lakebase Postgres database used to store Knowledge Graph triples. "
                "This is a separate database from the registry (configured in "
                "Settings → Lakebase → Connection). "
                "Three Postgres objects are created per domain+version during a "
                "Knowledge Graph build: a bulk table (<graph>_sync), a writable "
                "companion (<graph>__app) for reasoning and cohort writes, and a "
                "union view (<graph>) that readers query. "
                "Required grants: (1) USAGE on the graph schema (to connect); "
                "(2) CREATE on the graph schema (each rebuild creates a new set of "
                "objects); (3) SELECT / INSERT / UPDATE / DELETE on them. "
                "No extensions are required, and nothing outside the schema is "
                "touched — DROP SCHEMA ... CASCADE removes OntoBricks entirely."
            ),
            "checks": gdb_checks,
        }
    )
    all_checks.extend(gdb_checks)

    # ── Group 4: Delta Triple Store ────────────────────────────────────────
    delta_checks = [
        _safely_run(
            "delta.warehouse",
            "Delta warehouse — configured + reachable",
            lambda: _check_delta_warehouse(settings),
        ),
        _safely_run(
            "delta.objects",
            "Delta triple-store objects in UC schema",
            lambda: _check_delta_objects_exist(settings),
        ),
    ]
    groups.append(
        {
            "id": "delta",
            "title": "Lakehouse Triple Store",
            "description": (
                "Checks for the Lakehouse (Unity Catalog Delta) triple-store backend. "
                "When Lakehouse is selected in Settings → Back end → Lakehouse, OntoBricks "
                "stores triples as VIEW + Delta TABLE pairs inside the registry UC schema. "
                "A dedicated SQL warehouse can be configured in Settings → Lakehouse → SQL Warehouse "
                "for Lakehouse graph queries (falls back to the global warehouse if unset)."
            ),
            "checks": delta_checks,
        }
    )
    all_checks.extend(delta_checks)

    summary = {
        "total": len(all_checks),
        "ok": sum(1 for c in all_checks if c["status"] == _OK),
        "warnings": sum(1 for c in all_checks if c["status"] == _WARNING),
        "errors": sum(1 for c in all_checks if c["status"] == _ERROR),
    }
    overall = max(
        (c["status"] for c in all_checks),
        key=lambda s: _SEVERITY_RANK.get(s, 0),
        default=_OK,
    )
    return {
        "status": overall,
        "version": APP_VERSION,
        "summary": summary,
        "groups": groups,
    }


def _check_deployment_secrets(settings: Settings) -> tuple[str, str]:
    """Flag configuration that is safe locally but unsafe once deployed.

    Every field in :class:`Settings` has a default, so the app starts happily with
    nothing configured. That is right for local development and dangerous in a
    container: ``SECRET_KEY`` falls back to a literal published in this
    repository, so session cookies would be signed with a key anyone can read and
    therefore forge. Nothing warned about it before this check existed.

    Severity is deliberately conditional on ``ONTOBRICKS_CONTAINERIZED``: a
    developer running ``make run`` should not be nagged, and a deployment should
    not be able to hide it.
    """
    deployed = RuntimeEnv.is_containerized()
    problems: list[str] = []

    if settings.secret_key == _DEFAULT_SECRET_KEY:
        problems.append(
            "SECRET_KEY is the built-in default, which is published in the "
            "repository, so session cookies can be forged. Set it to a random value"
        )
    if deployed and not RuntimeEnv.secure_cookies():
        problems.append(
            "ONTOBRICKS_SECURE_COOKIES is off, so session cookies are sent over "
            "plain HTTP. Set it true behind TLS"
        )
    if deployed and not RuntimeEnv.auth_enabled():
        problems.append(
            "ONTOBRICKS_AUTH_ENABLED is false, so every request has full admin "
            "access. Only ever set that for local development"
        )

    if not problems:
        return _OK, (
            "SECRET_KEY set, cookies TLS-only, authentication enforced."
            if deployed
            else "No deployment-unsafe defaults in use."
        )

    return (_ERROR if deployed else _WARNING), "; ".join(problems) + "."


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def run_readiness_checks(settings: Settings | None = None) -> dict[str, Any]:
    """Execute every probe sequentially and roll up the worst severity.

    The function is synchronous so individual probes can use blocking
    SDK calls without ``await``. Wrap the whole thing in
    :func:`run_blocking` from an ``async`` route to keep the event
    loop free.
    """
    settings = settings or get_settings()

    checks: list[dict[str, Any]] = []
    checks.append(
        _safely_run(
            "runtime",
            "Application runtime",
            lambda: (
                _OK,
                f"Python {sys.version.split()[0]} — OntoBricks {APP_VERSION}",
            ),
        )
    )
    checks.append(
        _safely_run(
            "deployment.secrets",
            "Deployment-unsafe defaults",
            lambda: _check_deployment_secrets(settings),
        )
    )
    checks.append(_safely_run("filesystem.tmp", "/tmp writable + free space", _check_tmp))
    checks.append(
        _safely_run(
            "filesystem.session_dir",
            "Session directory writable",
            lambda: _check_session_dir(settings),
        )
    )
    checks.append(
        _safely_run("filesystem.log_dir", "Log directory writable", _check_log_dir)
    )
    checks.append(
        _safely_run("databricks.auth", "Databricks authentication", _check_databricks_auth)
    )
    checks.append(
        _safely_run(
            "databricks.warehouse",
            "SQL warehouse reachable",
            lambda: _check_warehouse(settings),
        )
    )
    checks.append(
        _safely_run(
            "databricks.cloudfetch",
            "CloudFetch capability",
            lambda: _check_cloud_fetch(settings),
        )
    )
    checks.append(
        _safely_run(
            "registry.cfg",
            "Registry configuration resolved",
            lambda: _check_registry_cfg(settings),
        )
    )
    checks.append(
        _safely_run(
            "registry.volume_read",
            "Registry UC volume — list",
            lambda: _check_registry_volume_read(settings),
        )
    )
    checks.append(
        _safely_run(
            "registry.volume_write",
            "Registry UC volume — write",
            lambda: _check_registry_volume_write(settings),
        )
    )
    checks.append(
        _safely_run(
            "registry.uc_schema_ddl",
            "Registry catalog/schema — view DDL",
            _check_registry_uc_schema_ddl,
        )
    )
    checks.append(
        _safely_run(
            "postgres",
            "PostgreSQL — Registry",
            lambda: _check_postgres(settings),
        )
    )
    checks.append(
        _safely_run(
            "postgres.permissions",
            "Lakebase — Registry permissions",
            lambda: _check_postgres_permissions(settings),
        )
    )
    checks.append(
        _safely_run(
            "graphdb.postgres",
            "Lakebase — Graph DB (separate database)",
            lambda: _check_graphdb_postgres(settings),
        )
    )

    summary = {
        "total": len(checks),
        "ok": sum(1 for c in checks if c["status"] == _OK),
        "warnings": sum(1 for c in checks if c["status"] == _WARNING),
        "errors": sum(1 for c in checks if c["status"] == _ERROR),
    }
    overall = max(
        (c["status"] for c in checks),
        key=lambda s: _SEVERITY_RANK.get(s, 0),
        default=_OK,
    )
    return {
        "status": overall,
        "version": APP_VERSION,
        "service": "OntoBricks",
        "framework": "FastAPI",
        "summary": summary,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/livez")
async def liveness_check():
    """Liveness probe — is this process serving? Touches nothing else.

    Deliberately separate from ``/health``, which runs every dependency check
    including live PostgreSQL and Databricks probes and can take many seconds.
    Pointing a Kubernetes liveness probe at that endpoint means a slow *external*
    dependency restarts a perfectly healthy app, and a restart cannot fix a
    database that is far away — it makes the outage worse by adding cold starts.

    This is also what a probe's default ``timeoutSeconds: 1`` can actually meet.
    An AKS startup probe against ``/health`` failed with ``context deadline
    exceeded`` and killed the container seven times before this existed.

    ``/health`` remains the endpoint for operators and monitoring, where the
    dependency detail is the point.
    """
    return {"status": "ok"}


@router.get("/health")
async def health_check(settings: Settings = Depends(get_settings)):
    """Readiness probe — returns ``200`` even when individual checks fail.

    External probes / load balancers should look at the top-level
    ``status`` field (``ok`` / ``warning`` / ``error``) and the
    ``summary.errors`` count. Returning a non-200 HTTP status would
    take the app out of rotation as soon as a *single* dependency
    flickered, which is rarely what you want for an analytical app.
    """
    return await run_blocking(run_readiness_checks, settings)
