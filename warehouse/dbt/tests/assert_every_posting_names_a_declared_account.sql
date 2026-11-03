-- Every GL posting must reference an account in the chart of accounts.
--
-- The chart is master data and the postings are documents, so this is ordinary
-- referential integrity -- but it is worth asserting because the relationship
-- did not exist until BTB-35. Account numbers were literals in the generator,
-- so a posting to an account nobody had declared was not merely possible, it
-- was indistinguishable from a correct one.
--
-- A posting to an undeclared account cannot be classified as an asset or an
-- expense, which means it silently drops out of both the balance sheet and the
-- income statement while still balancing its own document.
select
    p.accounting_document,
    p.fiscal_year,
    p.line_item,
    p.gl_account
from {{ ref('stg_gl_postings') }} as p
left join {{ ref('stg_chart_of_accounts') }} as c
    on p.gl_account = c.gl_account
where c.gl_account is null
