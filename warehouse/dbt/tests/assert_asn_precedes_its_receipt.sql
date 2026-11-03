-- An advance ship notice must ship before the goods arrive.
--
-- The invariant the whole extract exists to demonstrate, asserted PER ROW.
-- Comparing aggregates -- earliest ship date against earliest receipt date per
-- supplier, say -- would let a single violation hide inside a correct minimum.
-- That is the same failure mode as reconciling a grand total while two orders
-- offset each other, which is why CTRL-005 compares per order (ADR-001's
-- amendment).
--
-- Strictly before, not on or before: a shipment that arrives the day it ships
-- has no transit time, and an ASN that coincides with its receipt is not an
-- advance notice of anything.
select
    m.material_document,
    m.fiscal_year,
    m.material_document_item,
    m.inbound_delivery,
    h.shipped_date,
    m.posting_date,
    m.posting_date - h.shipped_date as transit_days
from {{ ref('stg_goods_movements') }} as m
inner join {{ ref('stg_shipment_headers') }} as h
    on m.inbound_delivery = h.inbound_delivery
where h.shipped_date >= m.posting_date
