-- Every accounting line must trace to the thing that caused it.
--
-- This is the join CTRL-003 and the lineage report both depend on. An
-- untraceable posting means a number on the dashboard with no provenance --
-- the thing this project exists to make impossible.
--
-- Three kinds of cause, not one. A posting traces to a material document, to a
-- revaluation document, or -- since ADR-028 -- to a production order, because
-- scrap and the work-in-process residual are value-only entries that no
-- material document causes: a scrapped unit is destroyed rather than moved,
-- and a residual is an arithmetic fact about an order.
--
-- Their references are prefixed SCRAP- and VARIANCE- and carry the order
-- number, so they are traceable to a real production order rather than being
-- exempted. Widening this to "or starts with SCRAP" would have been the easy
-- fix and would have accepted a reference pointing at nothing.
with source_documents as (
    select
        material_document as document_number,
        fiscal_year
    from {{ ref('fct_goods_movements') }}
    union
    select
        revaluation_document as document_number,
        fiscal_year
    from {{ ref('fct_inventory_revaluations') }}
),

order_costing_references as (
    -- The order must exist. A SCRAP- or VARIANCE- reference naming an order
    -- that was never raised is exactly as untraceable as a dangling material
    -- document, and the prefix must not buy it an exemption.
    select 'SCRAP-' || order_number as document_number
    from {{ ref('stg_production_orders') }}
    union
    select 'VARIANCE-' || order_number
    from {{ ref('stg_production_orders') }}
)

select
    p.accounting_document,
    p.fiscal_year,
    p.line_item,
    p.reference_document,
    p.gl_account,
    p.signed_amount_usd
from {{ ref('fct_gl_postings') }} as p
left join source_documents as d
    on
        p.reference_document = d.document_number
        and p.fiscal_year = d.fiscal_year
left join order_costing_references as o
    on p.reference_document = o.document_number
where
    d.document_number is null
    and o.document_number is null
