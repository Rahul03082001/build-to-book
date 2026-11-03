-- Production orders. One row per order.
with source as (
    select * from {{ source('raw', 'production_orders') }}
)

select
    order_number,
    material_number,
    plant                           as plant_code,
    target_quantity,
    upper(trim(uom))                as unit_of_measure,
    order_date,
    start_date,
    finish_date,
    to_char(finish_date, 'YYYY-MM') as finish_period,
    status,
    -- TECO means technically complete: the order met its target. Derived once
    -- here so no consumer has to remember which codes count as finished.
    status in ('CNF', 'TECO')       as is_confirmed,
    status = 'TECO'                 as is_complete,
    created_by,
    _batch_id                       as batch_id,
    _source_file                    as source_file,
    _file_sha256                    as source_file_sha256,
    _loaded_at                      as loaded_at
from source
