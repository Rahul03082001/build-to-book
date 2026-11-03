-- Inbound deliveries. One row per advance ship notice.
--
-- `days_late` is derived here, once, rather than in each model that needs it:
-- it is the difference between when the shipment arrived and when the supplier
-- promised it would. It is deliberately NOT turned into an on-time flag --
-- whether "on time" means exact or allows a tolerance window, and whether it
-- is measured by line or by shipment, are open questions recorded in
-- docs/metric_definitions.md for BTB-44. Publishing a flag here would answer
-- them by accident.
with source as (
    select * from {{ source('raw', 'shipment_headers') }}
)

select
    inbound_delivery,
    supplier_number,
    carrier_code,
    carrier_name,
    shipped_date,
    -- The supplier's commitment on the ASN: ship date plus the carrier's
    -- planned transit. Distinct from the purchase order's delivery date, which
    -- is what was agreed when the order was placed.
    promised_arrival_date,
    -- Null while in transit.
    arrived_date,
    planned_transit_days,
    status,
    status = 'RECEIVED'              as is_received,
    total_quantity_shipped,

    -- Positive means late, negative means early, null means not yet arrived.
    -- Null rather than zero: an unarrived shipment has no lateness yet, and
    -- zero would count it as on time.
    case
        when arrived_date is null then null
        else arrived_date - promised_arrival_date
    end                              as days_late,

    -- Actual transit, for comparison with what the carrier planned.
    case
        when arrived_date is null then null
        else arrived_date - shipped_date
    end                              as actual_transit_days,

    to_char(shipped_date, 'YYYY-MM') as shipped_period,
    to_char(arrived_date, 'YYYY-MM') as arrived_period,
    _batch_id                        as batch_id,
    _source_file                     as source_file,
    _file_sha256                     as source_file_sha256,
    _loaded_at                       as loaded_at
from source
