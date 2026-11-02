-- GRAIN: one row per supplier.
--
-- Type 1: supplier attributes are overwritten rather than versioned. Nothing
-- in this slice values anything at a supplier attribute, so there is no
-- restatement risk to protect against. If supplier terms ever drive a metric,
-- this needs the ADR-007 treatment.
select
    supplier_number,
    supplier_name,
    country_code,
    payment_terms,
    created_date,
    batch_id,
    source_file,
    loaded_at
from {{ ref('stg_suppliers') }}
