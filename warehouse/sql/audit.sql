-- Append-only audit trail (ADR-008).
--
-- One row per file per run in audit.pipeline_runs, carrying the evidence that
-- makes a reported number reproducible: which file, that file's checksum, how
-- many rows went in and came out, which commit of the code ran, and which
-- database role ran it.
--
-- Append-only is enforced at two layers, deliberately:
--
--   1. UPDATE, DELETE and TRUNCATE are revoked from every role.
--   2. A row-level trigger raises on any UPDATE or DELETE attempt.
--   3. A statement-level trigger raises on TRUNCATE.
--
-- The TRUNCATE guard is not redundant. TRUNCATE fires no row-level trigger, so
-- with only layers 1 and 2 the table owner could erase the whole audit trail in
-- one statement and leave nothing behind -- verified by doing it: eight rows
-- vanished silently before this trigger existed.
--
-- One layer alone is not enough. A revoke is bypassed by anything that gets a
-- privilege it should not have; a trigger alone is bypassed by ALTER TABLE
-- ... DISABLE TRIGGER. With both, a bypass leaves an error rather than a
-- silent edit. A superuser can still defeat both, which is documented as a
-- known limitation rather than papered over.
--
-- Run with:  make audit-schema

\set ON_ERROR_STOP on

-- Suppress the NOTICE noise from IF EXISTS on a first run.
SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- audit.pipeline_runs: one row per file per run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.pipeline_runs (
    run_id              uuid         NOT NULL,
    batch_id            text         NOT NULL,
    extract_name        text         NOT NULL,

    -- Provenance of the file itself. The checksum is what lets a reviewer
    -- prove the file behind a number has not changed since it was loaded.
    source_file         text         NOT NULL,
    file_sha256         char(64)     NOT NULL,

    -- Ties the data to the exact code that produced it.
    git_commit_sha      text         NOT NULL,

    -- Counts kept separately on purpose (ADR-020). "Skipped" means already
    -- present from an earlier run -- a routine rerun. "Rejected" means the row
    -- failed validation or duplicated its natural key inside the file -- an
    -- upstream fault. Collapsing them would make the duplicate-receipt
    -- control unable to fire.
    rows_in_file        integer      NOT NULL CHECK (rows_in_file >= 0),
    rows_inserted       integer      NOT NULL CHECK (rows_inserted >= 0),
    rows_skipped        integer      NOT NULL CHECK (rows_skipped >= 0),
    rows_rejected       integer      NOT NULL CHECK (rows_rejected >= 0),

    -- Declared by the source's control-totals sidecar (ADR-022), stored here
    -- so CTRL-001 can be re-tested from the audit trail alone.
    control_total_rows  integer,
    control_total_value numeric(20, 2),

    started_at          timestamptz  NOT NULL,
    ended_at            timestamptz,
    status              text         NOT NULL
        CHECK (status IN ('running', 'success', 'failed')),
    failure_reason      text,

    -- Which role ran it. Part of the evidence an access review needs.
    executed_by         text         NOT NULL DEFAULT current_user,

    recorded_at         timestamptz  NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, extract_name)
);

COMMENT ON TABLE audit.pipeline_runs IS
    'Append-only: one row per file per run. UPDATE and DELETE are revoked and a trigger raises on any attempt (ADR-008).';
COMMENT ON COLUMN audit.pipeline_runs.rows_skipped IS
    'Already present from an earlier run -- a routine rerun, not a fault.';
COMMENT ON COLUMN audit.pipeline_runs.rows_rejected IS
    'Failed validation or duplicated its natural key within the file -- an upstream fault.';

CREATE INDEX IF NOT EXISTS pipeline_runs_batch_idx
    ON audit.pipeline_runs (batch_id);
CREATE INDEX IF NOT EXISTS pipeline_runs_extract_started_idx
    ON audit.pipeline_runs (extract_name, started_at DESC);

-- ---------------------------------------------------------------------------
-- audit.rejected_rows: every row that did not land, with the reason.
--
-- A rejected row is never discarded (BTB-17). "We dropped 40 bad rows" is not
-- an answer anyone can act on; the row and the rule it broke are.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.rejected_rows (
    run_id          uuid        NOT NULL,
    batch_id        text        NOT NULL,
    extract_name    text        NOT NULL,
    source_file     text        NOT NULL,

    -- 1-based line number in the source file, so a person can open it and
    -- look. Excludes the header.
    source_row      integer     NOT NULL,

    -- Which rule rejected it, and the detail. Two kinds matter here:
    -- schema_violation (pandera) and duplicate_natural_key (ADR-020).
    rejection_rule  text        NOT NULL,
    rejection_detail text       NOT NULL,

    -- The row as it appeared in the file, so the reject is self-contained.
    raw_row         jsonb       NOT NULL,

    recorded_at     timestamptz NOT NULL DEFAULT now(),
    executed_by     text        NOT NULL DEFAULT current_user
);

COMMENT ON TABLE audit.rejected_rows IS
    'Rows that failed validation, with the rule they broke and the original row. Append-only.';

CREATE INDEX IF NOT EXISTS rejected_rows_run_idx
    ON audit.rejected_rows (run_id, extract_name);
CREATE INDEX IF NOT EXISTS rejected_rows_rule_idx
    ON audit.rejected_rows (rejection_rule);

-- ---------------------------------------------------------------------------
-- Append-only enforcement.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit.refuse_modification()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit.% is append-only: % is not permitted (ADR-008). '
        'Correct a mistake with a new run and a new batch id, not by editing history.',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

COMMENT ON FUNCTION audit.refuse_modification() IS
    'Second layer of append-only enforcement, behind the privilege revoke (ADR-008).';

DROP TRIGGER IF EXISTS pipeline_runs_append_only ON audit.pipeline_runs;
CREATE TRIGGER pipeline_runs_append_only
    BEFORE UPDATE OR DELETE ON audit.pipeline_runs
    FOR EACH ROW EXECUTE FUNCTION audit.refuse_modification();

DROP TRIGGER IF EXISTS rejected_rows_append_only ON audit.rejected_rows;
CREATE TRIGGER rejected_rows_append_only
    BEFORE UPDATE OR DELETE ON audit.rejected_rows
    FOR EACH ROW EXECUTE FUNCTION audit.refuse_modification();

-- TRUNCATE fires no row-level trigger, so it needs its own statement-level
-- guard. Without it the whole audit trail could be erased in one statement.
DROP TRIGGER IF EXISTS pipeline_runs_no_truncate ON audit.pipeline_runs;
CREATE TRIGGER pipeline_runs_no_truncate
    BEFORE TRUNCATE ON audit.pipeline_runs
    FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_modification();

DROP TRIGGER IF EXISTS rejected_rows_no_truncate ON audit.rejected_rows;
CREATE TRIGGER rejected_rows_no_truncate
    BEFORE TRUNCATE ON audit.rejected_rows
    FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_modification();

-- Layer one: no role may modify history. Granted INSERT and SELECT only.
REVOKE UPDATE, DELETE, TRUNCATE ON audit.pipeline_runs FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.rejected_rows FROM PUBLIC;

DO $$
DECLARE
    target_role text;
BEGIN
    FOREACH target_role IN ARRAY ARRAY['btb_loader', 'btb_transformer', 'btb_reporter', 'btb_auditor']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target_role) THEN
            EXECUTE format(
                'REVOKE UPDATE, DELETE, TRUNCATE ON audit.pipeline_runs, audit.rejected_rows FROM %I',
                target_role
            );
        END IF;
    END LOOP;
END
$$;

-- The loader and transformer record runs; the auditor reads them.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_loader') THEN
        GRANT INSERT, SELECT ON audit.pipeline_runs, audit.rejected_rows TO btb_loader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_transformer') THEN
        GRANT INSERT, SELECT ON audit.pipeline_runs, audit.rejected_rows TO btb_transformer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_auditor') THEN
        GRANT SELECT ON audit.pipeline_runs, audit.rejected_rows TO btb_auditor;
    END IF;
END
$$;
