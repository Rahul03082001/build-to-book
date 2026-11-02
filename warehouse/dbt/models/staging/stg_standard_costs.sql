-- Effective-dated standard costs. One window in force on any date (ADR-007).
with source as (
    select * from {{ source('raw', 'standard_costs') }}
)

select
    material_number,
    valid_from,
    valid_to,
    standard_cost_usd,
    currency                     as currency_code,
    approved_by,
    approved_on                  as approved_date,
    -- True for the window with no end date. Makes "current cost" a filter
    -- rather than a correlated subquery in every consumer.
    valid_to = date '9999-12-31' as is_current,
    _batch_id                    as batch_id,
    _source_file                 as source_file,
    _file_sha256                 as source_file_sha256,
    _loaded_at                   as loaded_at
from source
