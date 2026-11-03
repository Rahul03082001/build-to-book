-- The monthly trial balance. One row per account per period.
--
-- From the finance system (ADR-029), derived there from the same postings the
-- warehouse loads, so it reconciles to the cent on a clean run -- which is what
-- makes a disagreement a finding rather than noise.
--
-- Balances are signed in the debit direction throughout, so a period's closing
-- balances sum to zero across every account. A credit-balance account closes
-- negative, and that is correct rather than a fault.
with source as (
    select * from {{ source('raw', 'trial_balance') }}
)

select
    company_code,
    fiscal_year,
    posting_period,
    gl_account,
    opening_balance_usd,
    debits_usd,
    credits_usd,
    closing_balance_usd,
    -- The period's own movement, which is what reconciles against the
    -- postings. Closing minus opening isolates it from balances carried
    -- forward, so a disagreement points at the period that caused it rather
    -- than at every period after it.
    closing_balance_usd - opening_balance_usd as period_movement_usd,
    _batch_id                                 as batch_id,
    _source_file                              as source_file,
    _file_sha256                              as source_file_sha256,
    _loaded_at                                as loaded_at
from source
