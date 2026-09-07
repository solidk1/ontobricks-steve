"""Postgres flat triple store (subject/predicate/object rows, one graph per version).

The app owns every write. Inserts and deletes go through the FastAPI process via
psycopg — small ``executemany`` payloads, or ``COPY FROM STDIN`` for bulk.

Each graph version is three Postgres objects:

* ``*_sync``  -- bulk triples, streamed from the warehouse during a Build.
* ``*__app``  -- writable companion absorbing reasoning and cohort writes.
* ``*``       -- UNION view over both, which readers query. It carries the
  legacy single-table name, so callers are unaffected by the split.

The split is what lets a rebuild replace bulk triples without discarding
inferred ones. The ``_sync`` suffix is historical: it once denoted a table owned
by a Databricks Lakeflow synced-table pipeline. That ``managed_synced`` mode was
removed — it was Databricks-data-plane-only and had no equivalent on a generic
PostgreSQL server — but the physical suffix is retained so existing deployments
need no migration.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from back.core.errors import InfrastructureError
from back.core.graphdb.postgres import _companion_ddl
from back.core.graphdb.postgres.PostgresBase import PostgresBase
from back.core.helpers import validate_table_name
from back.core.logging import get_logger

logger = get_logger(__name__)

_BULK_INSERT_THRESHOLD = 50
_BULK_DELETE_THRESHOLD = 50

_COPY_INSERT_TEMP = "_ob_copy_stage"
_COPY_DELETE_TEMP = "_ob_del_stage"

# Postgres btree version-4 index entry limit (bytes).
_PG_BTREE_INDEX_MAX_BYTES = 2704

def _is_index_row_size_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "index row size" in msg and "btree" in msg:
        return True
    return exc.__class__.__name__ == "ProgramLimitExceeded"


def _long_literal_detail(batch: list[dict[str, str]]) -> str:
    worst = max(
        batch,
        key=lambda t: len((t.get("object") or "").encode("utf-8")),
    )
    obj_bytes = len((worst.get("object") or "").encode("utf-8"))
    predicate = worst.get("predicate") or "?"
    return f"predicate={predicate!r}, object_size={obj_bytes} bytes"


def _reraise_lakebase_index_limit(
    exc: BaseException, batch: list[dict[str, str]]
) -> None:
    if not _is_index_row_size_error(exc):
        raise exc
    detail = _long_literal_detail(batch)
    raise InfrastructureError(
        "Lakebase triple insert failed: a literal object exceeds the Postgres "
        f"btree index size limit ({detail}). Run a full Knowledge Graph rebuild "
        "to apply the object_hash schema, or exclude very long text columns "
        "from mapping."
    ) from exc


class PostgresFlatStore(PostgresBase):
    """Flat-model triple store on Lakebase Postgres.

    Each logical graph name resolves to a UNION view over the ``_sync`` bulk
    table and the ``__app`` companion, both under the configured Postgres
    *schema*. Direct writes go to the companion; bulk triples are streamed into
    ``_sync`` during a Build.

    ``search_path`` is set on every pooled connection so generated SQL from
    :class:`GraphDBBackend` helpers resolves correctly.
    """

    def __init__(
        self,
        auth: Any,
        schema: str,
        database_override: str = "",
    ) -> None:
        super().__init__(auth, schema, database_override)







    def _writable_table_id(self, name: str) -> str:
        """Return the Postgres table that direct app writes target.

        Reasoning and cohort writes go to the companion (``*__app``) so bulk
        warehouse data in ``*_sync`` is never modified after a Build.
        """
        return _companion_ddl.companion_phy(name)

    def _readable_table_id(self, name: str) -> str:
        """Return the view that direct reads should query.

        This is the union view, whose name equals the legacy single-table name.
        """
        return self.physical_table_id(name)

    def synced_table_name(self, table_name: str) -> str:
        """Return the ``_sync`` table name so callers can query without materialised triples."""
        return _companion_ddl.synced_phy(table_name)

    def get_inferred_triple_count(self, table_name: str) -> int:
        """Return the count of triples in the companion (reasoning / app-written) table.

        Queries ``*__app`` directly so callers can determine whether any
        inferred data exists independently of the union view total.
        Returns 0 on any error (e.g. companion table not yet created).
        """
        companion = _companion_ddl.companion_phy(table_name)
        try:
            return self.count_triples(companion)
        except Exception:
            return 0

    def synced_phy(self, name: str) -> str:
        """Postgres table name for the bulk side (``*_sync``, historical suffix)."""
        return _companion_ddl.synced_phy(name)

    def companion_phy(self, name: str) -> str:
        """Postgres table name for the writable companion (``*__app``)."""
        return _companion_ddl.companion_phy(name)







    def truncate_companion(self, name: str) -> None:
        """Empty the companion table, discarding inferred / cohort triples.

        Called on a full rebuild, where bulk triples are replaced wholesale and
        anything derived from the previous generation is stale.
        """
        companion = self.companion_phy(name)
        with self._cursor() as cur:
            _companion_ddl.truncate_companion(cur, companion)

    def _idx_name(self, phy: str, suffix: str) -> str:
        base = f"g_{phy}_{suffix}".lower()
        return base[:63]

    @staticmethod
    def _literal_meta(t: dict[str, Any]) -> tuple[str | None, str | None]:
        """Normalize optional RDF literal metadata for storage (NULL when absent)."""
        dt = t.get("datatype")
        lang = t.get("lang")
        if dt is not None and not isinstance(dt, str):
            dt = str(dt)
        if lang is not None and not isinstance(lang, str):
            lang = str(lang)
        dt_val = (dt or "").strip() or None
        lang_val = (lang or "").strip() or None
        return dt_val, lang_val

    @staticmethod
    def _row_to_triple(row: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {
            "subject": row["subject"] or "",
            "predicate": row["predicate"] or "",
            "object": row["object"] or "",
        }
        dt = row.get("datatype")
        lang = row.get("lang")
        if dt:
            out["datatype"] = str(dt)
        if lang:
            out["lang"] = str(lang)
        return out

    def _ensure_legacy_columns(self, cur: Any, phy: str) -> None:
        """Add ``datatype`` / ``lang`` when upgrading tables created before those columns."""
        cur.execute(
            f"ALTER TABLE {phy} ADD COLUMN IF NOT EXISTS datatype TEXT"
        )
        cur.execute(f"ALTER TABLE {phy} ADD COLUMN IF NOT EXISTS lang TEXT")
        self._warn_legacy_object_pk(cur, phy)

    @staticmethod
    def _warn_legacy_object_pk(cur: Any, phy: str) -> None:
        """Log when an existing table still keys on full ``object`` text."""
        bare = phy.split(".")[-1].strip('"')
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = ANY(current_schemas(false))
              AND table_name = %s
              AND column_name = 'object_hash'
            LIMIT 1
            """,
            (bare,),
        )
        if cur.fetchone() is None:
            logger.warning(
                "Lakebase table %s uses the legacy PRIMARY KEY (subject, predicate, object) "
                "and cannot store literal objects longer than %d bytes. "
                "Run a full Knowledge Graph rebuild to migrate to object_hash.",
                phy,
                _PG_BTREE_INDEX_MAX_BYTES,
            )

    @staticmethod
    def _require_pg():
        from back.core.graphdb.postgres.pool import _require_psycopg

        return _require_psycopg()

    def execute_query(self, query: str) -> list[dict[str, Any]]:
        _, dict_row = self._require_pg()
        pool = self._pool()
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f'SET search_path TO "{self._schema}"')
                cur.execute(query)
                if cur.description:
                    return [dict(row) for row in cur.fetchall()]
                return []

    def find_subjects_by_patterns(
        self, table_name: str, like_patterns: list[str]
    ) -> set[str]:
        """Optimized alias expansion for Lakebase Postgres.

        ``describe_entity`` may pass hundreds or thousands of ``%/<local-id>``
        patterns when expanding URI aliases. Building one giant OR-LIKE chain
        performs poorly and can hit statement timeouts.

        For fixed suffix patterns (``%/<id>``), group IDs by suffix length and
        build a single SQL statement with one ``RIGHT(subject, k) = ANY(...)``
        clause per distinct suffix length. This scales with distinct suffix
        lengths (typically 1-3), not with total pattern count, while keeping
        the lookup to a single round-trip.
        """
        if not like_patterns:
            return set()

        rel = self._sql_relation(table_name)
        by_suffix_len: dict[int, list[str]] = defaultdict(list)
        generic_patterns: list[str] = []

        for raw in like_patterns:
            p = (raw or "").strip()
            if not p:
                continue
            if p.startswith("%/") and ("%" not in p[2:]) and ("_" not in p[2:]):
                local_id = p[2:]
                suffix_len = len(local_id) + 1
                by_suffix_len[suffix_len].append(local_id)
            else:
                generic_patterns.append(p)

        out: set[str] = set()

        if by_suffix_len:
            or_clauses = []
            for suffix_len, ids in sorted(by_suffix_len.items()):
                array_literals = ", ".join(
                    f"'/{self._sql_escape(v)}'"
                    for v in sorted({value for value in ids if value})
                )
                or_clauses.append(
                    f"RIGHT(subject, {suffix_len}) = ANY(ARRAY[{array_literals}])"
                )
            rows = self.execute_query(
                f"SELECT DISTINCT subject FROM {rel} WHERE {' OR '.join(or_clauses)}"
            ) or []
            out.update(r["subject"] for r in rows if r.get("subject"))

        if generic_patterns:
            like_clauses = " OR ".join(
                f"subject LIKE '{self._sql_escape(p)}'" for p in generic_patterns
            )
            sql = f"SELECT DISTINCT subject FROM {rel} WHERE {like_clauses}"
            rows = self.execute_query(sql) or []
            out.update(r["subject"] for r in rows if r.get("subject"))

        return out

    def create_table(self, table_name: str) -> None:
        validate_table_name(table_name)
        # app_managed: create the full 3-object layout so reasoning / materialise
        # can write to the companion while bulk warehouse data lives in *_sync.
        synced = _companion_ddl.synced_phy(table_name)
        companion = _companion_ddl.companion_phy(table_name)
        view = self._readable_table_id(table_name)
        with self._cursor() as cur:
            _companion_ddl.ensure_synced(cur, self._schema, synced)
            try:
                self._ensure_legacy_columns(cur, synced)
            except Exception as _col_err:
                raise RuntimeError(
                    f"Table '{synced}' exists but is owned by a different database role "
                    f"— cannot add missing columns. "
                    f"Fix: as the owner of schema '{self._schema}' (or a member of "
                    f"that owning role), run: "
                    f"DROP TABLE IF EXISTS \"{self._schema}\".\"{synced}\" CASCADE;"
                ) from _col_err
            _companion_ddl.ensure_companion(cur, self._schema, companion)
            self._ensure_legacy_columns(cur, companion)
            _companion_ddl.ensure_union_view(cur, view, synced, companion)
        logger.info(
            "Lakebase graph layout ready: %s.[%s | %s | view %s]",
            self._schema,
            synced,
            companion,
            view,
        )

    def drop_table(self, table_name: str) -> None:
        validate_table_name(table_name)
        view = self._readable_table_id(table_name)
        companion = _companion_ddl.companion_phy(table_name)
        synced = _companion_ddl.synced_phy(table_name)
        with self._cursor() as cur:
            _companion_ddl.drop_view(cur, view)
            _companion_ddl.drop_companion(cur, companion)
            _companion_ddl.drop_synced(cur, synced)
        logger.info(
            "Dropped Lakebase graph layout %s.[%s | %s | view %s]",
            self._schema,
            synced,
            companion,
            view,
        )

    @contextmanager
    def _txn_cursor(self) -> Iterator[tuple[Any, Any]]:
        """Yield ``(conn, cur)`` inside an explicit transaction.

        The pool runs connections with ``autocommit=True`` so that read paths
        and small DDL/DML stay round-trip-cheap. Bulk paths that rely on
        ``ON COMMIT DROP`` temp tables (COPY-based insert / delete) need an
        explicit transaction boundary to keep the staging table alive across
        the COPY → INSERT/DELETE steps and ensure it is released when the
        block exits.
        """
        from back.core.graphdb.postgres.pool import _require_psycopg

        _, dict_row = _require_psycopg()
        pool = self._pool()
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(f'SET search_path TO "{self._schema}"')
                    yield conn, cur

    def _copy_insert_batch_phy(self, phy: str, batch: list[dict[str, str]]) -> int:
        """COPY *batch* into a temp staging table then ``INSERT … ON CONFLICT DO NOTHING``.

        Takes the resolved physical Postgres table name directly so callers can
        target either the writable companion (``*__app``) or the bulk-data sync
        table (``*_sync``) without going through ``_writable_table_id``.
        """
        if not batch:
            return 0
        try:
            with self._txn_cursor() as (_, cur):
                cur.execute(
                    f"CREATE TEMP TABLE {_COPY_INSERT_TEMP} ("
                    "subject TEXT, predicate TEXT, object TEXT, "
                    "datatype TEXT, lang TEXT) ON COMMIT DROP"
                )
                copy_sql = (
                    f"COPY {_COPY_INSERT_TEMP} "
                    "(subject, predicate, object, datatype, lang) FROM STDIN"
                )
                with cur.copy(copy_sql) as cp:
                    for t in batch:
                        dt, lg = self._literal_meta(t)
                        cp.write_row(
                            (
                                (t.get("subject", "") or ""),
                                (t.get("predicate", "") or ""),
                                (t.get("object", "") or ""),
                                dt,
                                lg,
                            )
                        )
                cur.execute(
                    f"INSERT INTO {phy} (subject, predicate, object, datatype, lang) "
                    f"SELECT subject, predicate, object, datatype, lang "
                    f"FROM {_COPY_INSERT_TEMP} ON CONFLICT DO NOTHING"
                )
        except Exception as exc:
            _reraise_lakebase_index_limit(exc, batch)
        return len(batch)

    def _copy_insert_batch(
        self, table_name: str, batch: list[dict[str, str]]
    ) -> int:
        """Route a COPY batch to the writable companion table for *table_name*."""
        if not batch:
            return 0
        validate_table_name(table_name)
        return self._copy_insert_batch_phy(self._writable_table_id(table_name), batch)

    def _copy_delete_batch(
        self, table_name: str, batch: list[dict[str, str]]
    ) -> int:
        """COPY *batch* into a temp staging table then ``DELETE … USING`` join.

        Replaces the per-row ``DELETE`` loop on the incremental remove path:
        the join executes server-side in Postgres so the app does not pay a
        round-trip per triple to be removed. In ``managed_synced`` mode the
        target is the writable companion (the synced side is immutable).
        """
        if not batch:
            return 0
        validate_table_name(table_name)
        phy = self._writable_table_id(table_name)
        with self._txn_cursor() as (_, cur):
            cur.execute(
                f"CREATE TEMP TABLE {_COPY_DELETE_TEMP} ("
                "subject TEXT, predicate TEXT, object TEXT) ON COMMIT DROP"
            )
            copy_sql = (
                f"COPY {_COPY_DELETE_TEMP} "
                "(subject, predicate, object) FROM STDIN"
            )
            with cur.copy(copy_sql) as cp:
                for t in batch:
                    cp.write_row(
                        (
                            (t.get("subject", "") or ""),
                            (t.get("predicate", "") or ""),
                            (t.get("object", "") or ""),
                        )
                    )
            cur.execute(
                f"DELETE FROM {phy} USING {_COPY_DELETE_TEMP} d "
                f"WHERE {phy}.subject = d.subject "
                f"AND {phy}.predicate = d.predicate "
                f"AND {phy}.object = d.object"
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def _insert_triples_executemany(
        self,
        table_name: str,
        triples: list[dict[str, str]],
        batch_size: int = 2000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Insert rows with ``executemany`` (small-payload fallback, < ``_BULK_INSERT_THRESHOLD``)."""
        validate_table_name(table_name)
        if not triples:
            return 0
        phy = self._writable_table_id(table_name)
        sql = (
            f"INSERT INTO {phy} (subject, predicate, object, datatype, lang) "
            f"VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
        )
        total = 0
        with self._cursor() as cur:
            for i in range(0, len(triples), batch_size):
                batch = triples[i : i + batch_size]
                rows = []
                for t in batch:
                    dt, lg = self._literal_meta(t)
                    rows.append(
                        (
                            (t.get("subject", "") or ""),
                            (t.get("predicate", "") or ""),
                            (t.get("object", "") or ""),
                            dt,
                            lg,
                        )
                    )
                cur.executemany(sql, rows)
                total += len(batch)
                if on_progress:
                    on_progress(total, len(triples))
        logger.info("Inserted %d triple rows into %s.%s", total, self._schema, phy)
        return total

    def insert_triples(
        self,
        table_name: str,
        triples: list[dict[str, str]],
        batch_size: int = 2000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        validate_table_name(table_name)
        if not triples:
            return 0
        if len(triples) >= _BULK_INSERT_THRESHOLD:
            return self.bulk_insert_iter(
                table_name,
                iter(triples),
                batch_size=batch_size,
                on_progress=on_progress,
            )
        return self._insert_triples_executemany(
            table_name, triples, batch_size=batch_size, on_progress=on_progress
        )

    def bulk_insert_iter(
        self,
        table_name: str,
        triple_iterator: Iterable[dict[str, str]],
        batch_size: int = 2000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Insert triples from an iterator in fixed-size batches via ``COPY FROM STDIN``.

        Bounded memory: only one ``batch_size`` window is held in RAM at any
        time. Each batch is its own transaction (COPY into ``_ob_copy_stage``
        then ``INSERT … ON CONFLICT DO NOTHING``) so progress callbacks fire
        per-batch and a single bad batch does not abort the entire load.
        """
        validate_table_name(table_name)
        batch: list[dict[str, str]] = []
        total = 0
        for t in triple_iterator:
            batch.append(t)
            if len(batch) >= batch_size:
                total += self._copy_insert_batch(table_name, batch)
                if on_progress:
                    on_progress(total, total)
                batch = []
        if batch:
            total += self._copy_insert_batch(table_name, batch)
            if on_progress:
                on_progress(total, total)
        if total:
            logger.info(
                "COPY-inserted %d triple rows into %s.%s",
                total,
                self._schema,
                self.physical_table_id(table_name),
            )
        return total

    def bulk_load_into_sync(
        self,
        table_name: str,
        triple_iterator: Iterable[dict[str, str]],
        batch_size: int = 5000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Bulk-load warehouse data into the ``*_sync`` table for *table_name*.

        Used by the ``app_managed`` full-rebuild path where the app streams
        triples from the Delta warehouse view directly into the sync table,
        mirroring what Lakeflow does automatically in ``managed_synced`` mode.

        Writes target ``synced_phy(table_name)`` (``*_sync``) so that
        post-build app writes (reasoning / materialise) continue to use the
        companion (``*__app``) via :meth:`_writable_table_id` and are not
        mixed with the warehouse snapshot.
        """
        validate_table_name(table_name)
        sync_phy = _companion_ddl.synced_phy(table_name)
        batch: list[dict[str, str]] = []
        total = 0
        for t in triple_iterator:
            batch.append(t)
            if len(batch) >= batch_size:
                total += self._copy_insert_batch_phy(sync_phy, batch)
                if on_progress:
                    on_progress(total, total)
                batch = []
        if batch:
            total += self._copy_insert_batch_phy(sync_phy, batch)
            if on_progress:
                on_progress(total, total)
        if total:
            logger.info(
                "COPY-loaded %d triple rows into %s.%s (sync table)",
                total,
                self._schema,
                sync_phy,
            )
        return total

    def bulk_delete_iter(
        self,
        table_name: str,
        triple_iterator: Iterable[dict[str, str]],
        batch_size: int = 2000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Delete triples from an iterator in fixed-size batches via temp-table JOIN.

        Mirror of :meth:`bulk_insert_iter` for the incremental remove path.
        """
        validate_table_name(table_name)
        batch: list[dict[str, str]] = []
        deleted = 0
        for t in triple_iterator:
            batch.append(t)
            if len(batch) >= batch_size:
                deleted += self._copy_delete_batch(table_name, batch)
                if on_progress:
                    on_progress(deleted, deleted)
                batch = []
        if batch:
            deleted += self._copy_delete_batch(table_name, batch)
            if on_progress:
                on_progress(deleted, deleted)
        if deleted:
            logger.info(
                "Bulk-deleted %d triple rows from %s.%s",
                deleted,
                self._schema,
                self.physical_table_id(table_name),
            )
        return deleted

    def delete_triples(
        self,
        table_name: str,
        triples: list[dict[str, str]],
        batch_size: int = 2000,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        validate_table_name(table_name)
        if not triples:
            return 0
        if len(triples) >= _BULK_DELETE_THRESHOLD:
            return self.bulk_delete_iter(
                table_name,
                iter(triples),
                batch_size=batch_size,
                on_progress=on_progress,
            )
        phy = self._writable_table_id(table_name)
        sql = f"DELETE FROM {phy} WHERE subject = %s AND predicate = %s AND object = %s"
        deleted = 0
        with self._cursor() as cur:
            for t in triples:
                cur.execute(
                    sql,
                    (
                        (t.get("subject", "") or ""),
                        (t.get("predicate", "") or ""),
                        (t.get("object", "") or ""),
                    ),
                )
                deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            if on_progress:
                on_progress(len(triples), len(triples))
        logger.info("Deleted %d triple rows from %s.%s", deleted, self._schema, phy)
        return deleted

    def query_triples(self, table_name: str) -> list[dict[str, str]]:
        validate_table_name(table_name)
        phy = self._readable_table_id(table_name)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT subject, predicate, object, datatype, lang FROM {phy} "
                f"ORDER BY subject, predicate"
            )
            rows = cur.fetchall()
        return [self._row_to_triple(r) for r in rows]

    def iter_triples(
        self,
        table_name: str,
        batch_size: int = 5000,
    ) -> Iterator[dict[str, str]]:
        """Yield triple rows in sort order without loading the full graph into memory."""
        validate_table_name(table_name)
        phy = self._readable_table_id(table_name)
        offset = 0
        while True:
            with self._cursor() as cur:
                cur.execute(
                    f"SELECT subject, predicate, object, datatype, lang FROM {phy} "
                    f"ORDER BY subject, predicate "
                    f"LIMIT %s OFFSET %s",
                    (batch_size, offset),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for r in rows:
                yield self._row_to_triple(r)
            offset += len(rows)
            if len(rows) < batch_size:
                break

    def count_triples(self, table_name: str) -> int:
        validate_table_name(table_name)
        phy = self._readable_table_id(table_name)
        with self._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {phy}")
            row = cur.fetchone()
        return int(row["cnt"]) if row else 0

    def table_exists(self, table_name: str) -> bool:
        if not table_name or not table_name.strip():
            return False
        phy = self._readable_table_id(table_name)
        with self._cursor() as cur:
            # In synced mode the readable target is a VIEW; check both
            # ``information_schema.tables`` and ``information_schema.views``
            # so the existence probe works in either mode.
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = %s
                UNION ALL
                SELECT 1 FROM information_schema.views
                WHERE table_schema = current_schema() AND table_name = %s
                LIMIT 1
                """,
                (phy, phy),
            )
            return cur.fetchone() is not None

    def get_status(self, table_name: str) -> dict[str, Any]:
        validate_table_name(table_name)
        count = self.count_triples(table_name)
        return {
            "count": count,
            "last_modified": None,
            "path": None,
            "format": "postgres",
            "schema": self._schema,
            "database": self._effective_database_display(),
        }

    def _effective_database_display(self) -> str:
        if self._database_override:
            return self._database_override
        try:
            return str(self._auth.database)
        except Exception:  # noqa: BLE001
            return ""

    def optimize_table(self, table_name: str) -> None:
        validate_table_name(table_name)
        # Both sides are app-written, so both benefit: *_sync was just
        # bulk-loaded and the companion accumulated reasoning writes.
        with self._cursor() as cur:
            cur.execute(f"VACUUM ANALYZE {self.synced_phy(table_name)}")
            cur.execute(f"VACUUM ANALYZE {self.companion_phy(table_name)}")


def resolve_postgres_graph_schema(
    domain: Any,
    settings: Any | None,
    config_schema: str,
) -> str:
    """Postgres schema segment for the graph triple tables.

    Precedence: an explicit ``graph_engine_config.schema`` always wins. With none
    set, the value is derived from the Unity Catalog Volume's schema segment
    (``RegistryCfg.schema``, the middle part of ``catalog.schema.volume``) so graph
    triples land under the same namespace as registry artefacts, then falls back to
    ``DEFAULT_GRAPH_SCHEMA``.

    The original reason for preferring the Volume's segment was the managed-synced
    write mode, which needed UC-registerable ``catalog.schema.table`` names. That
    mode was removed in v0.7.1; the alignment is kept because it keeps existing
    deployments' schema names stable, not because anything still requires it.
    """
    from back.core.graphdb.postgres.PostgresBase import (
        DEFAULT_GRAPH_SCHEMA,
        validate_graph_schema,
    )

    raw = (config_schema or "").strip()
    if raw:
        # Explicit schema configured in graph_engine_config — always honour it.
        return validate_graph_schema(raw)

    # No explicit schema: derive from the Registry Volume middle segment so
    # managed-sync UC names stay aligned with the registry UC namespace.
    try:
        from back.objects.registry import RegistryCfg

        rc = RegistryCfg.from_domain(domain, settings)
        reg_schema = (rc.schema or "").strip()
        if reg_schema:
            try:
                validated = validate_graph_schema(reg_schema)
            except ValueError as exc:
                logger.warning(
                    "Registry Volume schema %r is not a valid Lakebase identifier (%s) — "
                    "falling back to default graph schema",
                    reg_schema,
                    exc,
                )
            else:
                logger.info(
                    "Lakebase graph schema=%s from Registry Volume triplet "
                    "(graph_engine_config.schema was not set)",
                    validated,
                )
                return validated
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "resolve_postgres_graph_schema: registry unavailable: %s",
            exc,
        )
    return validate_graph_schema(DEFAULT_GRAPH_SCHEMA)




