-- CTRL-003's premise: each inventory account ties to the movements behind it.
--
-- Per account, not against 130000 alone. ADR-028 put stock in three accounts,
-- and a check that reconciled only raw materials would leave two thirds of
-- inventory uncovered while still passing -- the same failure shape as the
-- control that could not see production consumption at all (ADR-027).
--
-- Subledger is goods movements PLUS standard cost revaluations (ADR-023).
-- Omitting revaluations would make this pass while the inventory account
-- drifts away from quantity x standard cost.
--
-- Work in process is deliberately absent. It holds value but no quantity:
-- components issued to an order have no material document on the
-- work-in-process side, so there is no subledger of movements to tie it to.
-- Its own invariant is that it empties as orders close, which
-- `assert_work_in_process_empties_as_orders_close` asserts.
--
-- Compared to the cent. A tolerance here would mask the rounding faults the
-- money guards in the generator and loader exist to prevent; a real
-- materiality threshold is a separate decision (ADR-010).
with movements as (
    select
        stock_account,
        posting_period,
        sum(amount_usd) as amount_usd
    from {{ ref('fct_goods_movements') }}
    group by stock_account, posting_period
),

revaluations as (
    -- Revaluations restate raw material already on hand, so they belong to
    -- that account and nowhere else.
    select
        '130000'        as stock_account,
        posting_period,
        sum(amount_usd) as amount_usd
    from {{ ref('fct_inventory_revaluations') }}
    group by posting_period
),

subledger as (
    select
        stock_account,
        posting_period,
        sum(amount_usd) as subledger_usd
    from (
        select * from movements
        union all
        select * from revaluations
    ) as combined
    group by stock_account, posting_period
),

ledger as (
    select
        gl_account             as stock_account,
        posting_period,
        sum(signed_amount_usd) as ledger_usd
    from {{ ref('fct_gl_postings') }}
    where gl_account in ('130000', '137000')
    group by gl_account, posting_period
)

select
    coalesce(s.stock_account, l.stock_account)               as stock_account,
    coalesce(s.posting_period, l.posting_period)             as posting_period,
    s.subledger_usd,
    l.ledger_usd,
    coalesce(s.subledger_usd, 0) - coalesce(l.ledger_usd, 0) as difference_usd
from subledger as s
full outer join ledger as l
    on
        s.stock_account = l.stock_account
        and s.posting_period = l.posting_period
where coalesce(s.subledger_usd, 0) <> coalesce(l.ledger_usd, 0)
