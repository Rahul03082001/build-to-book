-- Access-control roles and grants for Build-to-Book.
--
-- Four roles with least privilege, so the access review in Phase 4 has
-- something real to report and so the Phase 1 tests can prove that each role
-- is denied what it should not be able to do.
--
--   loader       writes raw only              (ingestion)
--   transformer  reads raw, writes build      (dbt)
--   reporter     reads reporting only         (Streamlit, Tableau extracts)
--   auditor      reads audit and evidence     (reviewer, read-only)
--
-- Run with:  make roles     (wraps psql -v ON_ERROR_STOP=1 -f this file)
-- Passwords come from the environment; never hard-code them here.
--
-- Phase 0 establishes the roles and schema-level privileges. Phase 1 adds the
-- append-only enforcement on audit.pipeline_runs once that table exists.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Roles. NOLOGIN group roles carry the privileges; the LOGIN users inherit
-- them. That split is what makes a user access review meaningful: you review
-- role membership, not scattered per-user grants.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_loader') THEN
    CREATE ROLE btb_loader NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_transformer') THEN
    CREATE ROLE btb_transformer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_reporter') THEN
    CREATE ROLE btb_reporter NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_auditor') THEN
    CREATE ROLE btb_auditor NOLOGIN;
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Revoke the default PUBLIC privileges first. Without this, every role can
-- create objects in `public` and the least-privilege story is fiction.
-- ---------------------------------------------------------------------------
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- loader: writes raw, reads nothing else. Cannot touch reporting or audit
-- history, so a compromised loader cannot rewrite the audit trail.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE :"dbname" TO btb_loader;
GRANT USAGE, CREATE ON SCHEMA raw TO btb_loader;
GRANT USAGE ON SCHEMA audit TO btb_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO btb_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw GRANT USAGE, SELECT ON SEQUENCES TO btb_loader;
-- INSERT only on the audit trail: the loader records runs, never edits them.
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT INSERT ON TABLES TO btb_loader;

-- ---------------------------------------------------------------------------
-- transformer: reads raw, owns the build layers.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE :"dbname" TO btb_transformer;
GRANT USAGE ON SCHEMA raw TO btb_transformer;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw GRANT SELECT ON TABLES TO btb_transformer;

GRANT USAGE, CREATE ON SCHEMA staging TO btb_transformer;
GRANT USAGE, CREATE ON SCHEMA intermediate TO btb_transformer;
GRANT USAGE, CREATE ON SCHEMA marts TO btb_transformer;
GRANT USAGE, CREATE ON SCHEMA publish_candidate TO btb_transformer;
GRANT USAGE, CREATE ON SCHEMA sandbox TO btb_transformer;
-- The publish gate swaps schemas, so the transformer needs reporting DDL.
GRANT USAGE, CREATE ON SCHEMA reporting TO btb_transformer;
GRANT USAGE ON SCHEMA audit TO btb_transformer;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT INSERT ON TABLES TO btb_transformer;

-- ---------------------------------------------------------------------------
-- reporter: reads certified reporting tables only. This is the role the live
-- Streamlit console and the Tableau extract job connect as -- read-only, and
-- blind to raw, sandbox, and half-built candidates.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE :"dbname" TO btb_reporter;
GRANT USAGE ON SCHEMA reporting TO btb_reporter;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO btb_reporter;

-- ---------------------------------------------------------------------------
-- auditor: reads the audit trail and evidence packs. No access to business
-- data, which is the point -- an auditor tests the controls, not the numbers.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE :"dbname" TO btb_auditor;
GRANT USAGE ON SCHEMA audit TO btb_auditor;
GRANT USAGE ON SCHEMA evidence TO btb_auditor;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT ON TABLES TO btb_auditor;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence GRANT SELECT ON TABLES TO btb_auditor;

-- ---------------------------------------------------------------------------
-- Login users, one per role. Passwords are passed in as psql variables from
-- the environment by `make roles`.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_loader_svc') THEN
    CREATE ROLE btb_loader_svc LOGIN INHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_transformer_svc') THEN
    CREATE ROLE btb_transformer_svc LOGIN INHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_reporter_svc') THEN
    CREATE ROLE btb_reporter_svc LOGIN INHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_auditor_svc') THEN
    CREATE ROLE btb_auditor_svc LOGIN INHERIT;
  END IF;
END
$$;

ALTER ROLE btb_loader_svc      PASSWORD :'loader_password';
ALTER ROLE btb_transformer_svc PASSWORD :'transformer_password';
ALTER ROLE btb_reporter_svc    PASSWORD :'reporter_password';
ALTER ROLE btb_auditor_svc     PASSWORD :'auditor_password';

GRANT btb_loader      TO btb_loader_svc;
GRANT btb_transformer TO btb_transformer_svc;
GRANT btb_reporter    TO btb_reporter_svc;
GRANT btb_auditor     TO btb_auditor_svc;
