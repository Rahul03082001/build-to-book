-- The independent invariant behind the certified inventory value.
--
-- Closing value must equal on-hand quantity times the standard cost in force
-- at the end of the period. This is deliberately NOT the subledger-to-GL
-- reconciliation: that compares two sources which derive from the same
-- valuation, so a valuation error appears identically on both sides and the
-- reconciliation passes. A negative inventory value survived CTRL-003 exactly
-- that way before ADR-023 added revaluations.
--
-- Checked against the cost in force at the period end, not today's cost, so
-- earlier periods are tested on their own terms.
with balances as (
    select
        b.material_number,
        b.posting_period,
        b.closing_quantity,
        b.closing_value_usd,
        -- Last day of the period, which is the as-of date for valuation.
        (
            date_trunc('month', to_date(b.posting_period, 'YYYY-MM'))
            + interval '1 month - 1 day'
        )::date as period_end
    from {{ ref('mart_inventory_balance') }} as b
),

valued as (
    select
        balances.*,
        m.standard_cost_usd as cost_in_force
    from balances
    inner join {{ ref('dim_material') }} as m
        on m.material_number = balances.material_number
        -- As-of join: exactly one cost window covers any date (ADR-007).
        and balances.period_end between m.valid_from and m.valid_to
)

select
    material_number,
    posting_period,
    closing_quantity,
    closing_value_usd,
    cost_in_force,
    round(closing_quantity * cost_in_force, 2)                     as expected_value_usd,
    closing_value_usd - round(closing_quantity * cost_in_force, 2) as difference_usd
from valued
where closing_value_usd <> round(closing_quantity * cost_in_force, 2)
