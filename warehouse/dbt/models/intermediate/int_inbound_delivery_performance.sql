{{ config(materialized='table') }}

-- GRAIN: one row per inbound delivery line (inbound_delivery, shipment_item).
--
-- The keyed join ADR-026 exists for: a delivery line against the goods
-- receipts that arrived on it. The receipt carries the delivery number, so this
-- is a key join rather than a guess from purchase order line plus date -- which
-- matters because partial deliveries mean one order line can have two
-- shipments.
--
-- EVERY delivery line appears, received or not. An in-transit line is an open
-- commitment, not a missing receipt, and an inner join would delete exactly the
-- rows that make the distinction visible. The same reasoning as
-- int_mes_to_erp_completion, for the same reason.
--
-- This model exposes the INGREDIENTS of supplier on-time delivery and stops
-- short of the metric. Whether "on time" is exact or allows a tolerance window,
-- and whether it is measured by line or by shipment, are open questions
-- recorded in docs/metric_definitions.md against BTB-44. Both groupings are
-- available from this grain; a flag computed here would decide them silently.

with receipts_by_line as (
    select
        inbound_delivery,
        po_number,
        po_item,
        sum(quantity)     as quantity_received,
        min(posting_date) as first_receipt_date,
        max(posting_date) as last_receipt_date,
        count(*)          as receipt_line_count
    from {{ ref('stg_goods_movements') }}
    where
        movement_direction = 'RECEIPT'
        and inbound_delivery is not null
    group by inbound_delivery, po_number, po_item
),

ordered as (
    select
        po_number,
        po_item,
        quantity_ordered,
        -- What was agreed when the order was placed, as opposed to what the
        -- supplier later promised on the ASN. Lateness can be measured against
        -- either, and they disagree whenever a supplier re-promises.
        delivery_date as po_delivery_date
    from {{ ref('stg_purchase_order_lines') }}
)

select
    l.inbound_delivery,
    l.shipment_item,
    l.po_number,
    l.po_item,
    l.material_number,
    l.quantity_shipped,
    l.unit_of_measure,

    h.supplier_number,
    h.carrier_code,
    h.carrier_name,
    h.shipped_date,
    h.promised_arrival_date,
    h.arrived_date,
    h.planned_transit_days,
    h.actual_transit_days,
    h.status,
    h.is_received,

    o.quantity_ordered,
    o.po_delivery_date,

    coalesce(r.quantity_received, 0)                      as quantity_received,
    r.first_receipt_date,
    r.last_receipt_date,

    -- Against the supplier's ASN promise. Null until it arrives.
    h.days_late                                           as days_late_versus_promise,

    -- Against the purchase order's agreed date. A supplier can hit its own
    -- re-promised date and still be late against the order, which is why both
    -- are carried rather than one.
    case
        when h.arrived_date is null then null
        else h.arrived_date - o.po_delivery_date
    end                                                   as days_late_versus_order,

    -- Shipped minus received. Zero on every received line by construction
    -- (shipments are derived from receipts), so a non-zero value is a real
    -- finding rather than noise -- which is what makes it usable as a control
    -- later. On an in-transit line it equals the shipped quantity, because
    -- nothing has arrived.
    l.quantity_shipped - coalesce(r.quantity_received, 0) as quantity_in_transit,

    case
        when h.status = 'IN_TRANSIT' and r.inbound_delivery is null then 'IN_TRANSIT'
        when r.inbound_delivery is null then 'SHIPPED_NOT_RECEIVED'
        when l.quantity_shipped <> r.quantity_received then 'QUANTITY_DISCREPANCY'
        else 'RECONCILED'
    end                                                   as delivery_status,

    l.batch_id,
    l.source_file,
    l.source_file_sha256,
    l.loaded_at
from {{ ref('stg_shipment_lines') }} as l
inner join {{ ref('stg_shipment_headers') }} as h
    on l.inbound_delivery = h.inbound_delivery
left join ordered as o
    on
        l.po_number = o.po_number
        and l.po_item = o.po_item
left join receipts_by_line as r
    on
        l.inbound_delivery = r.inbound_delivery
        and l.po_number = r.po_number
        and l.po_item = r.po_item
