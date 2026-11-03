{{ config(materialized='table') }}

-- GRAIN: one row per purchase order line.
--
-- The three-way match (CTRL-004): PO line, goods receipt and supplier invoice
-- must agree on quantity and price. A supplier paid for goods never ordered or
-- never received is the risk this addresses, and it is one of the oldest
-- controls in accounts payable.
--
-- Every PO line appears, matched or not, because the unmatched ones are the
-- finding. An inner join would hide exactly the rows worth looking at:
--
--   * ordered but not received  -- open commitment, not yet a problem
--   * received but not invoiced -- a real GR/IR balance at period end
--   * invoiced but not received -- the serious one
--
-- Tolerances come from variables so the control's sensitivity is configuration
-- rather than a number buried in SQL. Price tolerance is a percentage because
-- a cent on a cable is noise and a cent on a GPU tray is not.

with po_lines as (
    select * from {{ ref('stg_purchase_order_lines') }}
),

receipts as (
    select
        po_number,
        po_item,
        sum(quantity)     as quantity_received,
        min(posting_date) as first_receipt_date,
        max(posting_date) as last_receipt_date,
        count(*)          as receipt_line_count
    from {{ ref('stg_goods_movements') }}
    where movement_direction = 'RECEIPT'
    group by po_number, po_item
),

invoiced as (
    select
        po_number,
        po_item,
        sum(quantity_invoiced)                                  as quantity_invoiced,
        sum(net_amount_usd)                                     as amount_invoiced_usd,
        -- Weighted average, because one PO line can be billed across
        -- several invoices at different prices.
        sum(net_amount_usd) / nullif(sum(quantity_invoiced), 0) as invoiced_unit_price_usd,
        count(*)                                                as invoice_line_count
    from {{ ref('stg_supplier_invoice_lines') }}
    group by po_number, po_item
)

select
    {{ dbt_utils.generate_surrogate_key(['p.po_number', 'p.po_item']) }} as match_key,
    p.po_number,
    p.po_item,
    p.material_number,
    p.plant_code,

    p.quantity_ordered,
    coalesce(r.quantity_received, 0)                                     as quantity_received,
    coalesce(i.quantity_invoiced, 0)                                     as quantity_invoiced,

    p.unit_price_usd                                                     as po_unit_price_usd,
    i.invoiced_unit_price_usd,
    p.net_value_usd                                                      as po_net_value_usd,
    coalesce(i.amount_invoiced_usd, 0)                                   as amount_invoiced_usd,

    r.first_receipt_date,
    r.last_receipt_date,
    coalesce(r.receipt_line_count, 0)                                    as receipt_line_count,
    coalesce(i.invoice_line_count, 0)                                    as invoice_line_count,

    -- The classification. Ordered deliberately: "invoiced without receipt" is
    -- checked before the quantity comparison, because an invoice for goods
    -- that never arrived is a different finding from one that arrived short.
    case
        when coalesce(i.quantity_invoiced, 0) > 0 and coalesce(r.quantity_received, 0) = 0
            then 'INVOICED_NOT_RECEIVED'
        when coalesce(r.quantity_received, 0) = 0
            then 'ORDERED_NOT_RECEIVED'
        when coalesce(i.quantity_invoiced, 0) = 0
            then 'RECEIVED_NOT_INVOICED'
        when
            abs(i.quantity_invoiced - r.quantity_received)
            > {{ var('three_way_match_quantity_tolerance') }}
            then 'QUANTITY_MISMATCH'
        when
            abs(i.invoiced_unit_price_usd - p.unit_price_usd)
            > p.unit_price_usd * {{ var('three_way_match_price_tolerance_pct') }}
            then 'PRICE_MISMATCH'
        else 'MATCHED'
    end                                                                  as match_status,

    i.quantity_invoiced - r.quantity_received                            as quantity_difference,
    i.invoiced_unit_price_usd
    - p.unit_price_usd
        as unit_price_difference_usd
from po_lines as p
left join receipts as r on p.po_number = r.po_number and p.po_item = r.po_item
left join invoiced as i on p.po_number = i.po_number and p.po_item = i.po_item
