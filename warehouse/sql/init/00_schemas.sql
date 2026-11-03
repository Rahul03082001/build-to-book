-- Bootstrap schemas. Runs once, on first `docker compose up`.
--
-- Layer purpose:
--   raw               landed source extracts, untransformed
--   staging/intermediate/marts   dbt build layers
--   publish_candidate validated build staged for the publish gate (ADR-014)
--   reporting         the certified dataset dashboards read
--   sandbox           risky changes and the failure demo; never touches reporting
--   audit             append-only run log, reconciliations, publish log (ADR-008)
--   evidence          generated IPE evidence packs

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS intermediate;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS publish_candidate;
CREATE SCHEMA IF NOT EXISTS reporting;
CREATE SCHEMA IF NOT EXISTS sandbox;

-- The failure demo rebuilds every dbt layer behind a `sandbox_` prefix, so it
-- cannot overwrite the production staging, intermediate or marts schemas. With
-- exact layer names a sandbox-targeted run put 14 models in `staging` and 8 in
-- `marts` and none in `sandbox`, while the workflow claimed the live data was
-- safe (warehouse/dbt/macros/generate_schema_name.sql).
CREATE SCHEMA IF NOT EXISTS sandbox_staging;
CREATE SCHEMA IF NOT EXISTS sandbox_intermediate;
CREATE SCHEMA IF NOT EXISTS sandbox_marts;
CREATE SCHEMA IF NOT EXISTS sandbox_dbt_test_failures;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS evidence;

COMMENT ON SCHEMA raw IS 'Landed source extracts with batch provenance columns. No business logic.';
COMMENT ON SCHEMA publish_candidate IS 'Validated build awaiting the publish gate. Swapped into reporting only if all critical checks pass (ADR-014).';
COMMENT ON SCHEMA reporting IS 'Certified dataset. Holds the last good dataset when a run fails.';
COMMENT ON SCHEMA sandbox IS 'Failure demos and risky changes. Never read by the live dashboards.';
COMMENT ON SCHEMA sandbox_marts IS 'The failure demo''s own marts. Proves the demo cannot touch production (BTB-67).';
COMMENT ON SCHEMA audit IS 'Append-only audit trail: pipeline runs, reconciliation results, publish log (ADR-008).';
