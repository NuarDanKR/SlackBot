-- B-12: revisioned pgvector candidate index.
--
-- This is a rebuildable cache. Markdown remains the source of truth and every
-- result must be reopened by doc_path/source_revision under the current ACL.
-- No source body, question, answer, credential, or provider response is stored.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS search_embedding_model_revision (
    revision          text PRIMARY KEY
                      CHECK (revision ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    provider          text NOT NULL CHECK (btrim(provider) <> ''),
    model             text NOT NULL CHECK (btrim(model) <> ''),
    input_price_per_mtok numeric CHECK (input_price_per_mtok >= 0),
    dimensions        integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    normalized        boolean NOT NULL DEFAULT true,
    state             text NOT NULL DEFAULT 'draft'
                      CHECK (state IN ('draft', 'active', 'retired')),
    external_approved boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL CHECK (btrim(created_by) <> '')
);

ALTER TABLE search_embedding_model_revision
    ADD COLUMN IF NOT EXISTS input_price_per_mtok numeric
    CHECK (input_price_per_mtok >= 0);

CREATE TABLE IF NOT EXISTS search_embedding_workspace_policy (
    workspace                  text PRIMARY KEY CHECK (btrim(workspace) <> ''),
    enabled                    boolean NOT NULL DEFAULT false,
    model_revision             text REFERENCES search_embedding_model_revision(revision),
    transport                  text NOT NULL DEFAULT 'local'
                               CHECK (transport IN ('local', 'external')),
    allow_channel              boolean NOT NULL DEFAULT true,
    allow_attachment           boolean NOT NULL DEFAULT true,
    allow_dm                   boolean NOT NULL DEFAULT false,
    external_transfer_approved boolean NOT NULL DEFAULT false,
    revision                   bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    reason                     text NOT NULL DEFAULT '' CHECK (length(reason) <= 500),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    updated_by                 text NOT NULL CHECK (btrim(updated_by) <> ''),
    CHECK (NOT enabled OR model_revision IS NOT NULL),
    CHECK (NOT external_transfer_approved OR transport = 'external')
);

CREATE TABLE IF NOT EXISTS archive_search_document_revision (
    id                bigserial PRIMARY KEY,
    workspace         text NOT NULL CHECK (btrim(workspace) <> ''),
    channel_id        text NOT NULL CHECK (btrim(channel_id) <> ''),
    doc_path          text NOT NULL CHECK (btrim(doc_path) <> ''),
    source_revision   text NOT NULL CHECK (source_revision ~ '^[0-9a-f]{64}$'),
    chunker_revision  text NOT NULL
                      CHECK (chunker_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:=-]{0,127}$'),
    model_revision    text NOT NULL REFERENCES search_embedding_model_revision(revision),
    state             text NOT NULL DEFAULT 'building'
                      CHECK (state IN ('building', 'ready', 'failed', 'retired')),
    is_current        boolean NOT NULL DEFAULT false,
    chunk_count       integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    indexed_at        timestamptz,
    error_code        text NOT NULL DEFAULT '' CHECK (length(error_code) <= 120),
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace, channel_id, doc_path, source_revision,
            chunker_revision, model_revision),
    CHECK (NOT is_current OR (state = 'ready' AND indexed_at IS NOT NULL))
);

-- A model revision may expose only one current source revision per document.
CREATE UNIQUE INDEX IF NOT EXISTS archive_search_document_one_current
    ON archive_search_document_revision
       (workspace, channel_id, doc_path, model_revision)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS archive_search_document_scope
    ON archive_search_document_revision (workspace, channel_id, model_revision)
    WHERE is_current AND state = 'ready';

CREATE TABLE IF NOT EXISTS archive_search_chunk (
    id                   bigserial PRIMARY KEY,
    document_revision_id bigint NOT NULL
                           REFERENCES archive_search_document_revision(id)
                           ON DELETE CASCADE,
    chunk_no             integer NOT NULL CHECK (chunk_no >= 0),
    start_offset         integer NOT NULL CHECK (start_offset >= 0),
    end_offset           integer NOT NULL CHECK (end_offset > start_offset),
    text_sha256          text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    embedding            vector NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_revision_id, chunk_no),
    UNIQUE (document_revision_id, start_offset, end_offset),
    CHECK (vector_dims(embedding) BETWEEN 1 AND 16000)
);

CREATE INDEX IF NOT EXISTS archive_search_chunk_document
    ON archive_search_chunk (document_revision_id, chunk_no);

CREATE OR REPLACE FUNCTION check_archive_search_chunk_dimensions()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected_dimensions integer;
BEGIN
    SELECT m.dimensions
      INTO expected_dimensions
      FROM archive_search_document_revision d
      JOIN search_embedding_model_revision m
        ON m.revision = d.model_revision
     WHERE d.id = NEW.document_revision_id;
    IF expected_dimensions IS NULL THEN
        RAISE EXCEPTION 'embedding_model_revision_missing'
            USING ERRCODE = '23514';
    END IF;
    IF vector_dims(NEW.embedding) <> expected_dimensions THEN
        RAISE EXCEPTION 'embedding_dimension_mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS archive_search_chunk_dimensions
    ON archive_search_chunk;
CREATE TRIGGER archive_search_chunk_dimensions
BEFORE INSERT OR UPDATE OF document_revision_id, embedding
ON archive_search_chunk
FOR EACH ROW EXECUTE FUNCTION check_archive_search_chunk_dimensions();

CREATE OR REPLACE FUNCTION check_archive_search_current_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actual_chunks integer;
BEGIN
    IF NEW.is_current THEN
        SELECT count(*) INTO actual_chunks
          FROM archive_search_chunk
         WHERE document_revision_id = NEW.id;
        IF actual_chunks <> NEW.chunk_count THEN
            RAISE EXCEPTION 'embedding_chunk_count_mismatch'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS archive_search_current_revision
    ON archive_search_document_revision;
CREATE TRIGGER archive_search_current_revision
BEFORE INSERT OR UPDATE OF is_current, state, chunk_count, indexed_at
ON archive_search_document_revision
FOR EACH ROW EXECUTE FUNCTION check_archive_search_current_revision();

COMMENT ON TABLE search_embedding_model_revision IS
    'Embedding model contract only; no credentials. Draft by default and never calls a provider.';
COMMENT ON TABLE search_embedding_workspace_policy IS
    'Workspace embedding authorization. Disabled by default; external transfer requires this and model approval.';
COMMENT ON TABLE archive_search_document_revision IS
    'Rebuildable vector-index manifest. Markdown remains authoritative; current ACL is rechecked.';
COMMENT ON TABLE archive_search_chunk IS
    'Embedding plus immutable source offsets/hash. Source text and answers are intentionally absent.';

REVOKE ALL ON TABLE search_embedding_model_revision FROM PUBLIC;
REVOKE ALL ON TABLE search_embedding_workspace_policy FROM PUBLIC;
REVOKE ALL ON TABLE archive_search_document_revision FROM PUBLIC;
REVOKE ALL ON TABLE archive_search_chunk FROM PUBLIC;
REVOKE ALL ON FUNCTION check_archive_search_chunk_dimensions() FROM PUBLIC;
REVOKE ALL ON FUNCTION check_archive_search_current_revision() FROM PUBLIC;

DO $$
DECLARE
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE search_embedding_model_revision,
                     search_embedding_workspace_policy,
                     archive_search_document_revision,
                     archive_search_chunk
            TO tyslackai;
        FOR seq_name IN
            SELECT quote_ident(n.nspname) || '.' || quote_ident(s.relname)
              FROM pg_class s
              JOIN pg_namespace n ON n.oid = s.relnamespace
              JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
              JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S'
               AND t.relname = ANY(ARRAY[
                   'archive_search_document_revision', 'archive_search_chunk'
               ])
        LOOP
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || seq_name || ' TO tyslackai';
        END LOOP;
    END IF;
END $$;

COMMIT;
