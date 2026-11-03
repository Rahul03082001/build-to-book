-- A goods issue must not reference an inbound delivery.
--
-- The other half of the nullability story. Asserting receipts are populated
-- says nothing about issues being empty, and a stamping bug that wrote the
-- delivery onto every row would pass that test while making every issue look
-- like it arrived from a supplier -- which would then be counted in delivery
-- performance.
select
    material_document,
    fiscal_year,
    material_document_item,
    movement_direction,
    inbound_delivery
from {{ ref('stg_goods_movements') }}
where
    movement_direction = 'ISSUE'
    and inbound_delivery is not null
