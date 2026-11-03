-- Supplier invoice items. One row per invoice line.
--
-- Each line references the PO line it pays for, which is what makes the
-- three-way match a join rather than a guess (CTRL-004).
with source as (
    select * from {{ source('raw', 'supplier_invoice_lines') }}
)

select
    invoice_number,
    fiscal_year,
    invoice_item,
    po_number,
    po_item,
    material_number,
    quantity_invoiced,
    unit_price_usd,
    net_amount_usd,
    upper(trim(uom)) as unit_of_measure,
    _batch_id        as batch_id,
    _source_file     as source_file,
    _file_sha256     as source_file_sha256,
    _loaded_at       as loaded_at
from source
