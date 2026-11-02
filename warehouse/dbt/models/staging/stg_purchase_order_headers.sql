-- Purchase order headers. One row per PO.
with source as (
    select * from {{ source('raw', 'purchase_order_headers') }}
)

select
    po_number,
    supplier_number,
    po_date,
    currency               as currency_code,
    created_by,
    company_code,
    _batch_id              as batch_id,
    _source_file           as source_file,
    _file_sha256           as source_file_sha256,
    _loaded_at             as loaded_at
from source
