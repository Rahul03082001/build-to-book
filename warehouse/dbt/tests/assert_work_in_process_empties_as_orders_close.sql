-- Work in process must net to zero once every order has closed.
--
-- The invariant that replaces a subledger tie for this account. Work in
-- process holds value but no quantity -- components issued to an order have no
-- material document on the work-in-process side -- so it cannot be reconciled
-- against movements the way raw materials and finished goods are (ADR-028).
--
-- What it CAN be held to is this: every order absorbs components and then
-- relieves the lot as finished goods, scrap and a residual variance. A
-- non-zero balance means an order absorbed cost that was never relieved, so
-- the value is sitting in an asset account with nothing to show for it.
--
-- Every production order finishes inside the reporting window by construction,
-- so there is no legitimate closing balance here. If orders were ever allowed
-- to span the period end, this test would need to exempt the open ones -- and
-- that exemption would have to be driven by the order's status, not by a
-- tolerance.
select
    gl_account,
    sum(signed_amount_usd) as balance_usd
from {{ ref('fct_gl_postings') }}
where gl_account = '135000'
group by gl_account
having sum(signed_amount_usd) <> 0
