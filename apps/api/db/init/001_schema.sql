-- DebugPilot schema.
--
-- This script is idempotent. Postgres runs it on first start (mounted into
-- /docker-entrypoint-initdb.d) and the API re-applies it on startup so an
-- existing volume picks up new tables and indexes.
--
-- The embedding dimension (768) matches nomic-embed-text. If EMBEDDING_MODEL
-- changes to a model with a different dimension, this column must change too.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS repositories (
    id                  BIGSERIAL PRIMARY KEY,
    url                 TEXT        NOT NULL,
    owner               TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    branch              TEXT,
    local_path          TEXT,
    status              TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'cloning', 'syncing', 'indexing', 'ready', 'error')),
    status_message      TEXT,
    current_commit_sha  TEXT,
    indexed_commit_sha  TEXT,
    last_synced_at      TIMESTAMPTZ,
    last_indexed_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One registration per URL + branch. A NULL branch means "default branch".
CREATE UNIQUE INDEX IF NOT EXISTS uq_repositories_url_branch
    ON repositories (url, COALESCE(branch, ''));

CREATE TABLE IF NOT EXISTS repository_syncs (
    id                    BIGSERIAL PRIMARY KEY,
    repository_id         BIGINT      NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    kind                  TEXT        NOT NULL CHECK (kind IN ('clone', 'sync')),
    status                TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    previous_commit_sha   TEXT,
    new_commit_sha        TEXT,
    commit_changed        BOOLEAN,
    changed_files         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    index_strategy        TEXT,
    index_plan            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    files_indexed         INTEGER,
    chunks_indexed        INTEGER,
    error                 TEXT,
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_repository_syncs_repository
    ON repository_syncs (repository_id, started_at DESC);

CREATE TABLE IF NOT EXISTS files (
    id              BIGSERIAL PRIMARY KEY,
    repository_id   BIGINT      NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    path            TEXT        NOT NULL,
    language        TEXT        NOT NULL,
    size_bytes      INTEGER     NOT NULL,
    line_count      INTEGER     NOT NULL,
    content_hash    TEXT        NOT NULL,
    commit_sha      TEXT,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repository_id, path)
);

CREATE TABLE IF NOT EXISTS code_chunks (
    id              BIGSERIAL PRIMARY KEY,
    repository_id   BIGINT      NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    file_id         BIGINT      NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    file_path       TEXT        NOT NULL,
    chunk_type      TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    language        TEXT        NOT NULL,
    start_line      INTEGER     NOT NULL,
    end_line        INTEGER     NOT NULL,
    content         TEXT        NOT NULL,
    content_hash    TEXT        NOT NULL,
    embedding       vector(768) NOT NULL,
    embedding_model TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_code_chunks_repository_file
    ON code_chunks (repository_id, file_path, start_line);
CREATE INDEX IF NOT EXISTS ix_code_chunks_file
    ON code_chunks (file_id);
CREATE INDEX IF NOT EXISTS ix_code_chunks_symbol
    ON code_chunks (repository_id, symbol);
CREATE INDEX IF NOT EXISTS ix_code_chunks_embedding_hnsw
    ON code_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_code_chunks_content_trgm
    ON code_chunks USING gin (content gin_trgm_ops);

CREATE TABLE IF NOT EXISTS debug_runs (
    id                            BIGSERIAL PRIMARY KEY,
    repository_id                 BIGINT      NOT NULL REFERENCES repositories (id) ON DELETE CASCADE,
    issue_description             TEXT        NOT NULL,
    setup_command                 TEXT,
    setup_command_approved_at     TIMESTAMPTZ,
    analysis_command              TEXT        NOT NULL,
    analysis_command_approved_at  TIMESTAMPTZ,
    status                        TEXT        NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'queued', 'running', 'completed', 'failed')),
    current_step                  TEXT,
    error                         TEXT,
    commit_sha                    TEXT,
    overall_result                TEXT,
    context_package               JSONB,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at                    TIMESTAMPTZ,
    finished_at                   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_debug_runs_repository
    ON debug_runs (repository_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_debug_runs_status
    ON debug_runs (status);

-- kind: 'runtime' (one per executed command), 'git', or 'code_chunk'.
CREATE TABLE IF NOT EXISTS evidence_records (
    id              BIGSERIAL PRIMARY KEY,
    debug_run_id    BIGINT      NOT NULL REFERENCES debug_runs (id) ON DELETE CASCADE,
    kind            TEXT        NOT NULL CHECK (kind IN ('runtime', 'git', 'code_chunk')),
    phase           TEXT,
    command         TEXT,
    exit_code       INTEGER,
    success         BOOLEAN,
    stdout          TEXT,
    stderr          TEXT,
    stack_trace     JSONB,
    data            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_evidence_records_run_kind
    ON evidence_records (debug_run_id, kind);

CREATE TABLE IF NOT EXISTS hypotheses (
    id                  BIGSERIAL PRIMARY KEY,
    debug_run_id        BIGINT      NOT NULL REFERENCES debug_runs (id) ON DELETE CASCADE,
    model               TEXT        NOT NULL,
    prompt              TEXT        NOT NULL,
    raw_response        TEXT        NOT NULL,
    summary             TEXT,
    root_cause          TEXT,
    file_path           TEXT,
    line_number         INTEGER,
    symbol              TEXT,
    introducing_commit  TEXT,
    confidence          TEXT,
    execution_path      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    missing_evidence    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    parse_ok            BOOLEAN     NOT NULL DEFAULT false,
    duration_ms         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_hypotheses_run
    ON hypotheses (debug_run_id);

CREATE TABLE IF NOT EXISTS validation_results (
    id              BIGSERIAL PRIMARY KEY,
    debug_run_id    BIGINT      NOT NULL REFERENCES debug_runs (id) ON DELETE CASCADE,
    hypothesis_id   BIGINT      NOT NULL REFERENCES hypotheses (id) ON DELETE CASCADE,
    overall_result  TEXT        NOT NULL CHECK (overall_result IN ('validated', 'needs_review')),
    checks          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_validation_results_run
    ON validation_results (debug_run_id);

CREATE TABLE IF NOT EXISTS ticket_drafts (
    id                  BIGSERIAL PRIMARY KEY,
    debug_run_id        BIGINT      NOT NULL REFERENCES debug_runs (id) ON DELETE CASCADE,
    provider            TEXT        NOT NULL DEFAULT 'mock-jira',
    ticket_key          TEXT        NOT NULL,
    title               TEXT        NOT NULL,
    summary             TEXT        NOT NULL,
    root_cause          TEXT,
    file_path           TEXT,
    line_number         INTEGER,
    reproduction        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    evidence            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    validation_status   TEXT,
    status              TEXT        NOT NULL DEFAULT 'draft',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ticket_drafts_run
    ON ticket_drafts (debug_run_id, created_at DESC);
