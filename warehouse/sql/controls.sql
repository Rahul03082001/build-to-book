-- Control results and the publish log (ADR-010, ADR-014, ADR-021).
--
-- Both tables are append-only for the same reason as audit.pipeline_runs
-- (ADR-008): a control result that can be edited after the fact is not
-- evidence. Enforced in the same three layers -- privilege revoke, a
-- row-level trigger on UPDATE/DELETE, and a statement-level trigger on
-- TRUNCATE, which fires no row trigger and would otherwise erase everything
-- in one statement.
--
-- Run with:  make controls-schema

\set ON_ERROR_STOP on

SET client_min_messages = warning;

-- ---------------------------------------------------------------------------
-- audit.reconciliation_results: one row per check per period per run.
--
-- Expected, actual, difference and tolerance are stored separately rather
-- than just a pass/fail, because "the reconciliation failed" is not something
-- anyone can act on. The difference is what gets investigated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.reconciliation_results (
    result_id       uuid        NOT NULL DEFAULT gen_random_uuid(),
    run_id          uuid        NOT NULL,
    batch_id        text        NOT NULL,

    check_id        text        NOT NULL,
    check_name      text        NOT NULL,
    -- Only 'critical' blocks a publish (ADR-010). A warning is recorded and
    -- surfaced but does not stop the refresh.
    severity        text        NOT NULL CHECK (severity IN ('critical', 'warning')),

    -- Null for a check that is not period-scoped, such as a row-count
    -- completeness check across the whole extract.
    period          text,
    -- Null for a check whose result is a count of exceptions rather than a
    -- comparison of two figures.
    expected_value  numeric(20, 2),
    actual_value    numeric(20, 2),
    difference      numeric(20, 2),
    tolerance       numeric(20, 2) NOT NULL DEFAULT 0,
    exception_count integer,

    passed          boolean     NOT NULL,
    -- Why it failed, in terms a person can act on.
    detail          text,

    target_schema   text        NOT NULL,
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz NOT NULL,
    executed_by     text        NOT NULL DEFAULT current_user,
    recorded_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (result_id)
);

COMMENT ON TABLE audit.reconciliation_results IS
    'Append-only: one row per check per period per run, with expected, actual, difference and tolerance.';
COMMENT ON COLUMN audit.reconciliation_results.severity IS
    'Only critical blocks a publish (ADR-010). A check that merely warns gets ignored.';

CREATE INDEX IF NOT EXISTS reconciliation_results_run_idx
    ON audit.reconciliation_results (run_id);
CREATE INDEX IF NOT EXISTS reconciliation_results_check_idx
    ON audit.reconciliation_results (check_id, started_at DESC);
CREATE INDEX IF NOT EXISTS reconciliation_results_failures_idx
    ON audit.reconciliation_results (passed, severity) WHERE NOT passed;

-- ---------------------------------------------------------------------------
-- audit.publish_log: one row per publish attempt, successful or blocked.
--
-- A blocked publish is the interesting row. It records which checks failed and
-- which dataset version `reporting` kept, so "why is the dashboard showing
-- yesterday?" has an answer that does not require reading logs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit.publish_log (
    publish_id        uuid        NOT NULL DEFAULT gen_random_uuid(),
    run_id            uuid        NOT NULL,
    batch_id          text        NOT NULL,
    git_commit_sha    text        NOT NULL,

    target_schema     text        NOT NULL,
    -- The version the candidate would become, and the one reporting is
    -- actually serving afterwards. On a block these differ, which is the
    -- whole point: reporting keeps the last good dataset (ADR-014).
    candidate_version text        NOT NULL,
    published_version text,
    previous_version  text,

    status            text        NOT NULL
        CHECK (status IN ('published', 'blocked', 'failed')),
    -- Which critical checks blocked it. Empty on success.
    blocking_checks   text[]      NOT NULL DEFAULT '{}',
    checks_run        integer     NOT NULL DEFAULT 0,
    checks_passed     integer     NOT NULL DEFAULT 0,
    reason            text,

    started_at        timestamptz NOT NULL,
    ended_at          timestamptz NOT NULL,
    executed_by       text        NOT NULL DEFAULT current_user,
    recorded_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (publish_id)
);

COMMENT ON TABLE audit.publish_log IS
    'Append-only: one row per publish attempt. A blocked row names the failing checks and the version reporting kept (ADR-014).';

CREATE INDEX IF NOT EXISTS publish_log_started_idx ON audit.publish_log (started_at DESC);
CREATE INDEX IF NOT EXISTS publish_log_status_idx ON audit.publish_log (status);

-- ---------------------------------------------------------------------------
-- Append-only enforcement, same three layers as audit.pipeline_runs.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS reconciliation_results_append_only ON audit.reconciliation_results;
CREATE TRIGGER reconciliation_results_append_only
    BEFORE UPDATE OR DELETE ON audit.reconciliation_results
    FOR EACH ROW EXECUTE FUNCTION audit.refuse_modification();

DROP TRIGGER IF EXISTS reconciliation_results_no_truncate ON audit.reconciliation_results;
CREATE TRIGGER reconciliation_results_no_truncate
    BEFORE TRUNCATE ON audit.reconciliation_results
    FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_modification();

DROP TRIGGER IF EXISTS publish_log_append_only ON audit.publish_log;
CREATE TRIGGER publish_log_append_only
    BEFORE UPDATE OR DELETE ON audit.publish_log
    FOR EACH ROW EXECUTE FUNCTION audit.refuse_modification();

DROP TRIGGER IF EXISTS publish_log_no_truncate ON audit.publish_log;
CREATE TRIGGER publish_log_no_truncate
    BEFORE TRUNCATE ON audit.publish_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.refuse_modification();

REVOKE UPDATE, DELETE, TRUNCATE
    ON audit.reconciliation_results, audit.publish_log FROM PUBLIC;

DO $$
DECLARE
    target_role text;
BEGIN
    FOREACH target_role IN ARRAY ARRAY['btb_loader', 'btb_transformer', 'btb_reporter', 'btb_auditor']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target_role) THEN
            EXECUTE format(
                'REVOKE UPDATE, DELETE, TRUNCATE ON audit.reconciliation_results, '
                'audit.publish_log FROM %I',
                target_role
            );
        END IF;
    END LOOP;

    -- The transformer runs the controls and the gate, so it records results.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_transformer') THEN
        GRANT INSERT, SELECT ON audit.reconciliation_results, audit.publish_log
            TO btb_transformer;
    END IF;
    -- The auditor reads them; that is the evidence it exists to test.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_auditor') THEN
        GRANT SELECT ON audit.reconciliation_results, audit.publish_log TO btb_auditor;
    END IF;
    -- The trust console shows check status beside each KPI, so the reporter
    -- needs to read results -- and nothing else in audit.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_reporter') THEN
        GRANT USAGE ON SCHEMA audit TO btb_reporter;
        GRANT SELECT ON audit.reconciliation_results, audit.publish_log TO btb_reporter;
    END IF;
END
$$;
