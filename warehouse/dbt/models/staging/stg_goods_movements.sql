-- Material document lines. One row per document item.
--
-- The movement type is translated into an explicit direction here, once.
-- Leaving `case movement_type when '101' then 1 ...` scattered through the
-- marts is how a new movement type silently gets a sign of zero and drops out
-- of the inventory balance.
with source as (
    select * from {{ source('raw', 'goods_movements') }}
)

select
    material_document,
    material_document_item,
    fiscal_year,
    movement_type,
    case movement_type
        when '101' then 'RECEIPT'
        when '261' then 'ISSUE'
    end                              as movement_direction,
    material_number,
    plant                            as plant_code,
    -- Already signed at source by movement type: receipts positive, issues
    -- negative.
    quantity,
    upper(trim(uom))                 as unit_of_measure,
    amount_usd,
    -- Posting date is the reporting basis; document date is when the
    -- paperwork was raised (docs/metric_definitions.md).
    posting_date,
    document_date,
    to_char(posting_date, 'YYYY-MM') as posting_period,
    po_number,
    po_item,
    entered_by,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
