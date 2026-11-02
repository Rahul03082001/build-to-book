"""Column-level lineage over dbt's compiled SQL, using sqlglot (ADR-009).

dbt's own graph is model-level: it shows that a mart depends on a staging
model, not that `inventory_value_usd` traces back to a particular source
column. A certified KPI needs the column-level path, which is what an auditor
asks for when testing a reported number.

Phase 5 emits `lineage/kpi_lineage.json` and a readable `kpi_lineage.md`,
driven by the dbt models tagged `meta: {certified: true}`.
"""
