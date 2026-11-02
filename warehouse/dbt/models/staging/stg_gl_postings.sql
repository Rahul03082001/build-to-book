-- Accounting document lines. One row per line item.
with source as (
    select * from {{ source('raw', 'gl_postings') }}
)

select
    accounting_document,
    line_item,
    fiscal_year,
    company_code,
    gl_account,
    debit_credit_indicator,
    case debit_credit_indicator
        when 'S' then 'DEBIT'
        when 'H' then 'CREDIT'
    end                              as posting_direction,
    amount_usd,
    -- Debits positive, credits negative, so a period's net movement on an
    -- account is a plain sum rather than a conditional one.
    signed_amount_usd,
    currency                         as currency_code,
    posting_date,
    document_date,
    to_char(posting_date, 'YYYY-MM') as posting_period,
    reference_document,
    material_number,
    entered_by,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
