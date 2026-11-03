-- Supplier invoice headers. One row per invoice.
with source as (
    select * from {{ source('raw', 'supplier_invoice_headers') }}
)

select
    invoice_number,
    fiscal_year,
    supplier_number,
    -- The supplier's own date on the document.
    invoice_date,
    -- When it hit the books. Reporting uses posting date
    -- (docs/metric_definitions.md).
    posting_date,
    to_char(posting_date, 'YYYY-MM') as posting_period,
    currency                         as currency_code,
    gross_amount_usd,
    payment_terms,
    entered_by,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
