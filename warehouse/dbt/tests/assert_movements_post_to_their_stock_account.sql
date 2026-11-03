-- Every material document must have a GL posting to the stock account its
-- movement says it affects.
--
-- `stg_goods_movements.stock_account` is the SQL side of the account
-- determination the generator applies when it writes the postings (ADR-028).
-- Two statements of the same rule can disagree, and if they did, CTRL-003
-- would reconcile each account against the wrong set of movements -- and still
-- balance overall, because the error would net out across accounts.
--
-- This holds them together. Checked per document rather than in aggregate: a
-- document posted to the wrong stock account is invisible in a total that
-- sums both accounts.
select
    m.material_document,
    m.fiscal_year,
    m.movement_direction,
    m.stock_account as expected_account
from {{ ref('stg_goods_movements') }} as m
where not exists (
    select 1
    from {{ ref('stg_gl_postings') }} as p
    where
        p.reference_document = m.material_document
        and p.fiscal_year = m.fiscal_year
        and p.gl_account = m.stock_account
)
