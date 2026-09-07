``back.core.graphdb.postgres`` — PostgreSQL graph engine
=========================================================

Flat triple tables on any PostgreSQL 14+ server — Azure Database for PostgreSQL,
RDS, self-hosted, or Databricks Lakebase, which is a Postgres endpoint like any
other. Used when a domain's ``graph_backend`` is ``postgres``; connection knobs
live under **Settings → Back end → PostgreSQL** and the ``PG*`` /
``ONTOBRICKS_PG_*`` environment.

See :doc:`../guides/postgres-graphdb` for the operator-facing guide.

Package
-------

.. automodule:: back.core.graphdb.postgres
   :members:
   :undoc-members:
   :show-inheritance:
   :no-index:

Base helpers
------------

.. automodule:: back.core.graphdb.postgres.PostgresBase
   :members:
   :undoc-members:
   :show-inheritance:

Flat store
----------

.. automodule:: back.core.graphdb.postgres.PostgresFlatStore
   :members:
   :undoc-members:
   :show-inheritance:

Bulk ingestion contract
~~~~~~~~~~~~~~~~~~~~~~~

The Knowledge Graph build pipeline never holds the full graph in memory. Triples
flow from the source warehouse to Postgres in fixed-size batches via the streaming
bulk paths:

* :py:meth:`back.core.graphdb.postgres.PostgresFlatStore.bulk_insert_iter`
  — per batch ``CREATE TEMP TABLE _ob_copy_stage … ON COMMIT DROP``,
  ``COPY FROM STDIN``, then
  ``INSERT INTO {phy} … SELECT FROM _ob_copy_stage ON CONFLICT DO NOTHING``.
* :py:meth:`back.core.graphdb.postgres.PostgresFlatStore.bulk_delete_iter`
  — symmetrical ``COPY`` into ``_ob_del_stage`` followed by
  ``DELETE FROM {phy} USING _ob_del_stage d WHERE …``, replacing the per-row
  ``DELETE`` loop on the incremental remove path.

Both run inside an explicit ``conn.transaction()`` because the graph-DB connection
pool uses ``autocommit=True``; ``ON COMMIT DROP`` would otherwise fire immediately
after the temp-table ``CREATE``.

Public ``insert_triples`` / ``delete_triples`` keep their signatures and delegate
to the bulk iterator paths once the payload crosses ``_BULK_INSERT_THRESHOLD`` /
``_BULK_DELETE_THRESHOLD`` (50 rows). Smaller payloads stay on the
``executemany`` / per-row fallback to avoid temp-table overhead for trivial diffs.

Split write / read surface
~~~~~~~~~~~~~~~~~~~~~~~~~~

Each graph version is three objects in the configured schema:

* ``g_<dom>_v<n>_sync`` — bulk triples streamed from the warehouse during a Build.
  The ``_sync`` suffix is historical: it once denoted a table owned by a Databricks
  Lakeflow synced-table pipeline. That ``managed_synced`` mode was removed in
  v0.7.1 along with the Apps deploy; the name was kept so existing deployments'
  objects stay addressable.
* ``g_<dom>_v<n>__app`` — writable companion populated by reasoning and cohort
  writes, so bulk warehouse data is never modified after a Build.
* ``g_<dom>_v<n>`` — UNION view (the back-compat name) that all readers query.

:py:class:`back.core.graphdb.postgres.PostgresFlatStore` routes direct writes
(``insert_triples``, ``delete_triples``, the COPY bulk paths) to the companion via
``_writable_table_id``, and reads (``query_triples``, ``count_triples``,
``iter_triples``, ``table_exists``, ``get_status``) to the union view via
``_readable_table_id``. SPARQL and KG-search code paths see no difference.

Companion, union view and hash DDL
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. automodule:: back.core.graphdb.postgres._companion_ddl
   :members:
   :undoc-members:
   :show-inheritance:

Uniqueness is keyed on a generated ``object_hash BYTEA`` column rather than the
``object`` literal, because btree cannot index TEXT beyond ~2704 bytes. The hash
comes from an **in-schema** ``sha256_utf8()`` function OntoBricks creates itself
— not ``pgcrypto``. Two reasons: ``CREATE EXTENSION`` is database-scoped and would
survive ``DROP SCHEMA … CASCADE`` (breaking the co-tenancy contract, and on Azure
requiring a server-level allowlist change), and ``convert_to`` is ``STABLE`` while
a generated column requires ``IMMUTABLE``, so ``sha256(convert_to(...))`` inline is
rejected outright.

Connection pool
---------------

.. automodule:: back.core.graphdb.postgres.pool
   :members:
   :undoc-members:
   :show-inheritance:
