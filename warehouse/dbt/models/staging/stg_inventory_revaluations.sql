-- Standard cost revaluations: value-only entries, no quantity (ADR-023).
--
-- Part of the inventory subledger alongside goods movements. Omitting these
-- lets the inventory account drift away from quantity x standard cost while
-- the subledger-to-GL reconciliation still passes, because movements and GL
-- postings share one valuation.
with source as (
    select * from {{ source('raw', 'inventory_revaluations') }}
)

select
    revaluation_document,
    fiscal_year,
    material_number,
    posting_date,
    to_char(posting_date, 'YYYY-MM')   as posting_period,
    old_standard_cost_usd,
    new_standard_cost_usd,
    new_standard_cost_usd - old_standard_cost_usd as cost_delta_usd,
    quantity_on_hand,
    amount_usd,
    currency                           as currency_code,
    approved_by,
    _batch_id                          as batch_id,
    _source_file                       as source_file,
    _file_sha256                       as source_file_sha256,
    _loaded_at                         as loaded_at
from source
