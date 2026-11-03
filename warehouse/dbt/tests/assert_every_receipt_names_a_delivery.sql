-- Every goods receipt must name the inbound delivery it arrived on.
--
-- `inbound_delivery` is nullable, because an issue has no supplier shipment
-- and a receipt whose purchase order does not exist gets no delivery -- the
-- `receipt_without_po` defect plants exactly that and has to reach the
-- warehouse for CTRL-004 to find it.
--
-- Those legitimate nulls are the problem: a silent population failure would
-- look identical to them. So this asserts the narrow case where a delivery is
-- mandatory -- a receipt against a purchase order that exists -- rather than
-- testing the column is non-null, which would fail on the defect run.
select
    m.material_document,
    m.fiscal_year,
    m.material_document_item,
    m.po_number,
    m.po_item
from {{ ref('stg_goods_movements') }} as m
inner join {{ ref('stg_purchase_order_lines') }} as p
    on
        m.po_number = p.po_number
        and m.po_item = p.po_item
where
    m.movement_direction = 'RECEIPT'
    and m.inbound_delivery is null
