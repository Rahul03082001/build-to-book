-- CTRL-003's premise: the inventory subledger ties to GL account 130000.
--
-- Subledger is goods movements PLUS standard cost revaluations (ADR-023).
-- Omitting revaluations would make this pass while the inventory account
-- drifts away from quantity x standard cost.
--
-- Compared to the cent. A tolerance here would mask the rounding faults the
-- money guards in the generator and loader exist to prevent; a real
-- materiality threshold is a separate decision (ADR-010).
with movements as (
    select posting_period, sum(amount_usd) as amount_usd
    from {{ ref('fct_goods_movements') }}
    group by 1
),

revaluations as (
    select posting_period, sum(amount_usd) as amount_usd
    from {{ ref('fct_inventory_revaluations') }}
    group by 1
),

subledger as (
    select posting_period, sum(amount_usd) as subledger_usd
    from (
        select * from movements
        union all
        select * from revaluations
    ) combined
    group by 1
),

ledger as (
    select posting_period, sum(signed_amount_usd) as ledger_usd
    from {{ ref('fct_gl_postings') }}
    where gl_account = '130000'
    group by 1
)

select
    coalesce(s.posting_period, l.posting_period) as posting_period,
    s.subledger_usd,
    l.ledger_usd,
    coalesce(s.subledger_usd, 0) - coalesce(l.ledger_usd, 0) as difference_usd
from subledger s
full outer join ledger l on l.posting_period = s.posting_period
where coalesce(s.subledger_usd, 0) <> coalesce(l.ledger_usd, 0)
