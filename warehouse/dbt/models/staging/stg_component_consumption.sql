-- Components issued to production orders. One row per consumption line.
with source as (
    select * from {{ source('raw', 'component_consumption') }}
)

select
    order_number,
    consumption_item,
    component_material_number,
    quantity_consumed,
    upper(trim(uom))                 as unit_of_measure,
    movement_type,
    posting_date,
    to_char(posting_date, 'YYYY-MM') as posting_period,
    entered_by,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
