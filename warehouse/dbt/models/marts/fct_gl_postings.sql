-- GRAIN: one row per accounting document line (accounting_document,
-- fiscal_year, line_item).
select
    {{ dbt_utils.generate_surrogate_key([
        'accounting_document', 'fiscal_year', 'line_item'
    ]) }} as gl_posting_key,
    accounting_document,
    line_item,
    fiscal_year,
    company_code,
    gl_account,
    debit_credit_indicator,
    posting_direction,
    amount_usd,
    signed_amount_usd,
    posting_date,
    document_date,
    posting_period,
    -- Links back to the material document or revaluation document that caused
    -- the posting -- the join CTRL-003 relies on.
    reference_document,
    material_number,
    entered_by,
    batch_id,
    source_file,
    source_file_sha256,
    loaded_at
from {{ ref('stg_gl_postings') }}
