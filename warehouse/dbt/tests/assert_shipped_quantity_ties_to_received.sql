-- A received delivery line must have shipped exactly what arrived.
--
-- Exact on a clean run because shipments are derived from the receipts
-- (ADR-026), which is what makes this usable as a control baseline later: a
-- discrepancy is a finding rather than noise. Generating the two sides
-- independently would leave a gap made of sampling error, and a control whose
-- baseline is noise cannot detect anything (the lesson CTRL-005 rests on).
--
-- In-transit lines are excluded, not because they are uninteresting but
-- because nothing has arrived on them yet: shipped minus received equals the
-- shipped quantity by definition, which is an open commitment and not a
-- discrepancy.
select
    inbound_delivery,
    shipment_item,
    po_number,
    po_item,
    quantity_shipped,
    quantity_received,
    quantity_in_transit
from {{ ref('int_inbound_delivery_performance') }}
where
    delivery_status <> 'IN_TRANSIT'
    and quantity_shipped <> quantity_received
