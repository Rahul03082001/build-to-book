{{ config(
    materialized='table',
    meta={'certified': true, 'owner': 'Finance'}
) }}

-- GRAIN: one row per material per posting period.
--
-- CERTIFIED KPI: inventory value.
-- Definition: docs/metric_definitions.md.
--
-- Movement in the period, and the closing balance carried forward. The
-- closing balance is cumulative, so it answers "what was on hand at the end
-- of August?" rather than "what moved during August?" -- two questions that
-- get confused constantly and differ by the opening balance.
--
-- Value is goods movements PLUS standard cost revaluations (ADR-023). A
-- revaluation moves value without moving quantity, so the quantity and value
-- columns deliberately draw on different sources: closing_quantity comes from
-- movements alone, closing_value_usd from both.
--
-- The invariant that holds this honest, tested below and in the Python suite:
--
--     closing_value_usd == closing_quantity x current standard cost
--
-- It is asserted independently of CTRL-003, because the subledger-to-GL
-- reconciliation cannot detect a valuation error that both of its sources
-- share -- which is exactly how a negative inventory value survived it once.

with periods as (
    select distinct posting_period from {{ ref('dim_date') }} d
    inner join {{ ref('fct_goods_movements') }} f on f.posting_period = d.period
),

-- Every material x period combination, so a period in which a material did
-- not move still carries its balance forward. Without this, a material that
-- stopped moving silently vanishes from the report rather than showing the
-- stock it still holds.
scaffold as (
    select
        m.material_number,
        p.posting_period
    from (select distinct material_number from {{ ref('fct_goods_movements') }}) m
    cross join periods p
),

movement_by_period as (
    select
        material_number,
        posting_period,
        sum(quantity)                                              as movement_quantity,
        sum(amount_usd)                                            as movement_value_usd,
        sum(case when movement_direction = 'RECEIPT' then quantity else 0 end)
                                                                   as receipt_quantity,
        sum(case when movement_direction = 'RECEIPT' then amount_usd else 0 end)
                                                                   as receipt_value_usd,
        sum(case when movement_direction = 'ISSUE' then -quantity else 0 end)
                                                                   as issue_quantity,
        sum(case when movement_direction = 'ISSUE' then -amount_usd else 0 end)
                                                                   as issue_value_usd,
        count(*)                                                   as movement_line_count
    from {{ ref('fct_goods_movements') }}
    group by 1, 2
),

revaluation_by_period as (
    select
        material_number,
        posting_period,
        sum(amount_usd) as revaluation_value_usd,
        count(*)        as revaluation_count
    from {{ ref('fct_inventory_revaluations') }}
    group by 1, 2
),

combined as (
    select
        s.material_number,
        s.posting_period,
        coalesce(m.movement_quantity, 0)                as movement_quantity,
        coalesce(m.movement_value_usd, 0)               as movement_value_usd,
        coalesce(m.receipt_quantity, 0)                 as receipt_quantity,
        coalesce(m.receipt_value_usd, 0)                as receipt_value_usd,
        coalesce(m.issue_quantity, 0)                   as issue_quantity,
        coalesce(m.issue_value_usd, 0)                  as issue_value_usd,
        coalesce(m.movement_line_count, 0)              as movement_line_count,
        coalesce(r.revaluation_value_usd, 0)            as revaluation_value_usd,
        coalesce(r.revaluation_count, 0)                as revaluation_count
    from scaffold s
    left join movement_by_period m
        on  m.material_number = s.material_number
        and m.posting_period = s.posting_period
    left join revaluation_by_period r
        on  r.material_number = s.material_number
        and r.posting_period = s.posting_period
),

running as (
    select
        material_number,
        posting_period,
        movement_quantity,
        movement_value_usd,
        receipt_quantity,
        receipt_value_usd,
        issue_quantity,
        issue_value_usd,
        movement_line_count,
        revaluation_value_usd,
        revaluation_count,
        movement_value_usd + revaluation_value_usd as period_value_change_usd,
        sum(movement_quantity) over material_to_date  as closing_quantity,
        sum(movement_value_usd + revaluation_value_usd) over material_to_date
                                                      as closing_value_usd
    from combined
    window material_to_date as (
        partition by material_number
        order by posting_period
        rows between unbounded preceding and current row
    )
)

select
    {{ dbt_utils.generate_surrogate_key(['r.material_number', 'r.posting_period']) }}
        as inventory_balance_key,
    r.material_number,
    r.posting_period,
    d.material_group,
    d.material_description,
    d.base_unit_of_measure,
    d.current_standard_cost_usd,

    -- Period activity.
    r.receipt_quantity,
    r.receipt_value_usd,
    r.issue_quantity,
    r.issue_value_usd,
    r.movement_quantity,
    r.movement_value_usd,
    r.revaluation_value_usd,
    r.revaluation_count,
    r.period_value_change_usd,
    r.movement_line_count,

    -- Closing position, cumulative to the end of the period.
    r.closing_quantity,
    r.closing_value_usd,

    -- Opening position, for a reader who wants the movement to reconcile:
    -- opening + change = closing.
    r.closing_quantity - r.movement_quantity                as opening_quantity,
    r.closing_value_usd - r.period_value_change_usd          as opening_value_usd,

    -- Exception flag for the Inventory dashboard page. Physically impossible,
    -- so clean data never sets it (CTRL-010).
    r.closing_quantity < 0                                   as is_negative_on_hand
from running r
inner join {{ ref('dim_material_current') }} d
    on d.material_number = r.material_number
