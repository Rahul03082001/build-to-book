-- Inbound delivery items. One row per purchase order line per delivery.
--
-- Merged at source so (inbound_delivery, po_number, po_item) is unique: a
-- delivery carrying two receipt splits of one order line becomes a single item
-- with the combined quantity, as SAP's LIPS would. That uniqueness is what a
-- shipped-versus-received reconciliation joins on.
with source as (
    select * from {{ source('raw', 'shipment_lines') }}
)

select
    inbound_delivery,
    shipment_item,
    po_number,
    po_item,
    material_number,
    -- Never negative: a shipped quantity is a count of goods on a truck,
    -- unlike a movement quantity, which is signed by movement type.
    quantity_shipped,
    upper(trim(uom)) as unit_of_measure,
    _batch_id        as batch_id,
    _source_file     as source_file,
    _file_sha256     as source_file_sha256,
    _loaded_at       as loaded_at
from source
