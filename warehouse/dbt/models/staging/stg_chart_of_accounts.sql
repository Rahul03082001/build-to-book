-- The chart of accounts. One row per general ledger account.
--
-- Master data from finance (BTB-35). Every posting references an account, and
-- before this the account numbers lived as constants in Python with their type
-- in a trailing comment -- so nothing could ask whether a posting was to an
-- asset or an expense, and a balance sheet could not be separated from an
-- income statement.
with source as (
    select * from {{ source('raw', 'chart_of_accounts') }}
)

select
    gl_account,
    account_name,
    account_type,
    -- DEBIT or CREDIT: the side that increases this account. Carried so a
    -- posting's direction can be read against what the account normally does.
    normal_balance,
    is_balance_sheet,
    not is_balance_sheet as is_income_statement,
    account_purpose,
    _batch_id            as batch_id,
    _source_file         as source_file,
    _file_sha256         as source_file_sha256,
    _loaded_at           as loaded_at
from source
