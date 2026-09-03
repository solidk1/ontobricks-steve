-- Reference DDL for one OntoBricks flat triple table on PostgreSQL.
-- Replace <schema> and <table> with validated identifiers (see LakebaseFlatStore).
--
-- Requires NO extensions. `sha256()` and `gen_random_uuid()` are core as of
-- PG 11 and PG 13 respectively, so the PG 14 floor needs nothing installed.
--
-- The hash goes through an IMMUTABLE wrapper rather than being inlined:
-- `convert_to()` is STABLE, and PostgreSQL requires generation expressions to
-- be IMMUTABLE, so `sha256(convert_to(object, 'UTF8'))` is rejected outright
-- with "generation expression is not immutable". Marking the wrapper IMMUTABLE
-- is sound because a text value's UTF-8 encoding is deterministic;
-- `convert_to`'s STABLE marking is conservative, reflecting that it reads
-- `server_encoding`, which is fixed for the life of a database.
--
-- The wrapper is created *inside* the OntoBricks schema so that
-- `DROP SCHEMA "<schema>" CASCADE` removes it too, leaving no residue in a
-- database shared with other tenants. This is also why pgcrypto's `digest()`
-- is no longer used: an extension is database-scoped, survives the schema
-- drop, and on Azure Database for PostgreSQL requires an instance-wide
-- `azure.extensions` allowlist change.

CREATE SCHEMA IF NOT EXISTS "<schema>";

CREATE OR REPLACE FUNCTION "<schema>".sha256_utf8(t TEXT) RETURNS BYTEA
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $ob$ SELECT sha256(convert_to(t, 'UTF8')) $ob$;

CREATE TABLE IF NOT EXISTS "<schema>"."<table>" (
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    object_hash BYTEA GENERATED ALWAYS AS ("<schema>".sha256_utf8(coalesce(object, ''))) STORED,
    datatype    TEXT,
    lang        TEXT,
    PRIMARY KEY (subject, predicate, object_hash)
);

CREATE INDEX IF NOT EXISTS ix_<table>_sp  ON "<schema>"."<table>" (subject, predicate);
CREATE INDEX IF NOT EXISTS ix_<table>_po  ON "<schema>"."<table>" (predicate, object_hash);
CREATE INDEX IF NOT EXISTS ix_<table>_oph ON "<schema>"."<table>" (object_hash, predicate);
