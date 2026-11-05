-- B-12: authorization for embedding a *question*, kept apart from indexing.
--
-- Indexing embeds archived source text that already passed the archive's PII
-- rules and a reviewer.  A question is typed by a person into Slack seconds
-- earlier: it has passed nothing.  Treating one approval as the other would
-- mean that approving the index quietly started shipping people's questions to
-- an external provider, and nobody would see a row change to show it.
--
-- So this is its own table, with its own switch, its own transfer approval,
-- its own revision counter and its own approver.  Both switches are off by
-- default.  No question, answer, embedding, or credential is stored here.

BEGIN;

CREATE TABLE IF NOT EXISTS search_embedding_query_policy (
    workspace                        text PRIMARY KEY
                                     CHECK (workspace ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    -- May this workspace embed a question at all?  Off by default: a
    -- workspace that is indexing is still not querying until someone says so.
    allow_query                      boolean NOT NULL DEFAULT false,
    -- Questions may only be embedded with the model whose vectors the index
    -- was built with; a different revision compares numbers that mean
    -- different things.
    model_revision                   text
                                     REFERENCES search_embedding_model_revision(revision),
    transport                        text NOT NULL DEFAULT 'local'
                                     CHECK (transport IN ('local', 'external')),
    -- Deliberately NOT named external_transfer_approved.  The index table has
    -- a column by that name, and a query path that reads the wrong one would
    -- still compile, still pass, and still send questions out.
    query_external_transfer_approved boolean NOT NULL DEFAULT false,
    revision                         bigint NOT NULL DEFAULT 1
                                     CHECK (revision > 0),
    reason                           text NOT NULL DEFAULT ''
                                     CHECK (length(reason) <= 500),
    updated_at                       timestamptz NOT NULL DEFAULT now(),
    updated_by                       text NOT NULL CHECK (btrim(updated_by) <> ''),
    CHECK (NOT allow_query OR model_revision IS NOT NULL),
    CHECK (NOT query_external_transfer_approved OR transport = 'external')
);

COMMENT ON TABLE search_embedding_query_policy IS
    'Workspace question-embedding authorization. Separate from the index policy on purpose: off by default, external transfer needs this row and the model row. Stores no question text.';
COMMENT ON COLUMN search_embedding_query_policy.query_external_transfer_approved IS
    'Approval to send question text to an external provider. The index policy approval never stands in for this one.';

REVOKE ALL ON TABLE search_embedding_query_policy FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE search_embedding_query_policy
            TO tyslackai;
    END IF;
END $$;

COMMIT;
