-- Every accounting line must trace to the material document or revaluation
-- document that caused it.
--
-- This is the join CTRL-003 and the lineage report both depend on. An
-- untraceable posting means a number on the dashboard with no provenance --
-- the thing this project exists to make impossible.
with source_documents as (
    select material_document as document_number, fiscal_year
    from {{ ref('fct_goods_movements') }}
    union
    select revaluation_document as document_number, fiscal_year
    from {{ ref('fct_inventory_revaluations') }}
)

select
    p.accounting_document,
    p.fiscal_year,
    p.line_item,
    p.reference_document,
    p.gl_account,
    p.signed_amount_usd
from {{ ref('fct_gl_postings') }} p
left join source_documents d
    on  d.document_number = p.reference_document
    and d.fiscal_year = p.fiscal_year
where d.document_number is null
