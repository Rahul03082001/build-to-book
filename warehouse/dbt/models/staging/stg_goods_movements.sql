-- Material document lines. One row per document item.
--
-- The movement type is translated into an explicit direction here, once.
-- Leaving `case movement_type when '101' then 1 ...` scattered through the
-- marts is how a new movement type silently gets a sign of zero and drops out
-- of the inventory balance.
with source as (
    select * from {{ source('raw', 'goods_movements') }}
)

select
    material_document,
    material_document_item,
    fiscal_year,
    movement_type,
    case
        -- A receipt against a production order is confirmed output, not a
        -- purchase. SAP uses 101 for both and tells them apart by the
        -- reference, which is exactly what this does (ADR-028). Separated here
        -- so anything counting purchases cannot silently include production.
        when movement_type = '101' and order_number is not null then 'PRODUCTION_RECEIPT'
        when movement_type = '601' then 'DISPATCH'
        when movement_type = '101' then 'RECEIPT'
        when movement_type = '261' then 'ISSUE'
        -- Opening stock (ADR-027). A separate direction rather than folded in
        -- with receipts, because it is not a purchase: there is no purchase
        -- order, no supplier and no GR/IR entry behind it, and a report that
        -- counted it as a receipt would overstate the period's purchasing.
        when movement_type = '561' then 'OPENING'
    end                              as movement_direction,

    -- Which inventory account this movement moves value into or out of
    -- (ADR-028). Stock sits in three accounts now, so CTRL-003 has to
    -- reconcile per account -- reconciling only raw materials would leave two
    -- thirds of inventory uncovered and still pass.
    --
    -- This is the SQL side of the account determination the generator applies
    -- when it writes the postings. `assert_movements_post_to_their_stock_account`
    -- holds the two together, so this cannot drift into being a second,
    -- disagreeing opinion.
    case
        when movement_type = '601' then '137000'
        when movement_type = '101' and order_number is not null then '137000'
        else '130000'
    end                              as stock_account,
    material_number,
    plant                            as plant_code,
    -- Already signed at source by movement type: receipts positive, issues
    -- negative.
    quantity,
    upper(trim(uom))                 as unit_of_measure,
    amount_usd,
    -- Posting date is the reporting basis; document date is when the
    -- paperwork was raised (docs/metric_definitions.md).
    posting_date,
    document_date,
    to_char(posting_date, 'YYYY-MM') as posting_period,
    po_number,
    po_item,
    -- The production order an issue was consumed by (ADR-027). Null on
    -- receipts, so order costing joins on this and purchase analysis joins on
    -- po_number; a movement carries exactly one of the two.
    order_number,
    -- The inbound delivery this receipt arrived on (ADR-026). Null on issues,
    -- which have no supplier shipment -- so a join to shipments must be an
    -- outer one, and `is_inbound_receipt` says which rows can have a match.
    inbound_delivery,
    inbound_delivery is not null     as has_inbound_delivery,
    entered_by,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
