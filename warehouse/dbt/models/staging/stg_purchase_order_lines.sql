-- Purchase order items. One row per PO line -- a different grain from the
-- header, which is why `count(*)` over purchase orders is ambiguous until a
-- model says which it means.
with source as (
    select * from {{ source('raw', 'purchase_order_lines') }}
)

select
    po_number,
    po_item,
    material_number,
    quantity_ordered,
    unit_price_usd,
    net_value_usd,
    upper(trim(uom))       as unit_of_measure,
    delivery_date,
    plant                  as plant_code,
    _batch_id              as batch_id,
    _source_file           as source_file,
    _file_sha256           as source_file_sha256,
    _loaded_at             as loaded_at
from source
