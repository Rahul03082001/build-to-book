{{ config(materialized='table') }}

-- GRAIN: one row per production order.
--
-- CTRL-005: units MES says passed final test, against units ERP confirmed.
-- The two systems count a finished unit at different moments, and the gap
-- between them is the finding -- which is why this model reports the
-- difference rather than asserting the two are equal.
--
-- A unit "completed" in MES means it passed final test on some attempt. A
-- reworked unit that eventually passes is complete; it is simply not a
-- first-pass success (that distinction belongs to yield, not completion).
--
-- EVERY order appears, confirmed or not. An order with MES activity and no
-- confirmation is the serious case -- units built and never reported to
-- finance -- and an inner join would hide exactly that.

with mes_completions as (
    select
        order_number,
        count(distinct serial_number) filter (where passed and is_final_step)
            as mes_units_passed_final_test,
        count(distinct serial_number)       as mes_units_seen,
        max(event_date)                     as last_event_date,
        to_char(max(started_at), 'YYYY-MM') as last_event_period
    from {{ ref('stg_mes_unit_events') }}
    group by order_number
),

confirmations as (
    select
        order_number,
        sum(confirmed_quantity) as erp_confirmed_quantity,
        sum(scrap_quantity)     as erp_scrap_quantity,
        max(posting_date)       as last_confirmation_date
    from {{ ref('stg_production_confirmations') }}
    group by order_number
)

select
    o.order_number,
    o.material_number,
    o.target_quantity,
    o.finish_period,

    coalesce(m.mes_units_passed_final_test, 0) as mes_units_completed,
    coalesce(m.mes_units_seen, 0)              as mes_units_seen,
    coalesce(c.erp_confirmed_quantity, 0)      as erp_units_confirmed,
    coalesce(c.erp_scrap_quantity, 0)          as erp_units_scrapped,

    -- The control's figure. Positive means MES built units ERP never
    -- confirmed, which is the direction that matters: output not reported to
    -- finance.
    coalesce(m.mes_units_passed_final_test, 0)
    - coalesce(c.erp_confirmed_quantity, 0)    as completion_gap,

    m.last_event_date,
    c.last_confirmation_date,

    case
        when m.order_number is null then 'NO_MES_ACTIVITY'
        when c.order_number is null then 'MES_WITHOUT_CONFIRMATION'
        when
            coalesce(m.mes_units_passed_final_test, 0)
            <> coalesce(c.erp_confirmed_quantity, 0) then 'COMPLETION_GAP'
        else 'RECONCILED'
    end                                        as completion_status
from {{ ref('stg_production_orders') }} as o
left join mes_completions as m on o.order_number = m.order_number
left join confirmations as c on o.order_number = c.order_number
