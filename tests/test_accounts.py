"""The chart of accounts (BTB-35).

Account numbers used to be four module constants in `inventory.py` with the
type carried in a trailing comment. Nothing could ask whether a posting was to
an asset or an expense, so a balance sheet could not be separated from an
income statement, and `debit_credit_indicator` could not be checked against
what the account normally does.

The tests that matter here are the ones tying the declaration to the postings.
A chart that lists accounts nobody posts to, or postings to accounts nobody
declared, is worse than no chart: it looks like a data dictionary and is not.
"""

from __future__ import annotations

import collections

import pytest

from generator import accounts, dataset
from generator import config as genconfig


def test_every_account_is_declared_once() -> None:
    numbers = [account.number for account in accounts.ACCOUNTS]
    duplicated = [n for n, c in collections.Counter(numbers).items() if c > 1]
    assert not duplicated, f"account(s) declared twice: {duplicated}"
    assert len(accounts.BY_NUMBER) == len(accounts.ACCOUNTS)


def test_accounts_are_in_number_order() -> None:
    """The extract reads like a chart of accounts rather than like a dict."""
    numbers = [account.number for account in accounts.ACCOUNTS]
    assert numbers == sorted(numbers), f"out of order: {numbers}"


def test_every_account_declares_a_type_and_a_normal_balance() -> None:
    for account in accounts.ACCOUNTS:
        assert account.account_type in {
            accounts.ASSET,
            accounts.LIABILITY,
            accounts.EQUITY,
            accounts.EXPENSE,
        }
        assert account.normal_balance in {accounts.DEBIT, accounts.CREDIT}
        assert len(account.purpose) > 30, (
            f"{account.number} has no usable purpose; it becomes the column "
            "description in the warehouse, so restating the name is no use"
        )


def test_the_normal_balance_follows_the_account_type() -> None:
    """Assets and expenses increase by debit; liabilities and equity by credit.

    Not a style rule -- it is what the type means. An asset whose normal
    balance were CREDIT would make every sign convention downstream wrong.
    """
    expected = {
        accounts.ASSET: accounts.DEBIT,
        accounts.EXPENSE: accounts.DEBIT,
        accounts.LIABILITY: accounts.CREDIT,
        accounts.EQUITY: accounts.CREDIT,
    }
    for account in accounts.ACCOUNTS:
        assert account.normal_balance == expected[account.account_type], (
            f"{account.number} is a {account.account_type} with a "
            f"{account.normal_balance} normal balance"
        )


def test_balance_sheet_and_income_statement_partition_the_chart() -> None:
    """Every account is on one statement or the other, never both or neither."""
    on_balance_sheet = {a.number for a in accounts.ACCOUNTS if a.is_balance_sheet}
    on_income = {a.number for a in accounts.ACCOUNTS if not a.is_balance_sheet}
    assert on_balance_sheet and on_income
    assert not on_balance_sheet & on_income
    assert on_balance_sheet | on_income == set(accounts.BY_NUMBER)


def test_the_three_inventory_accounts_are_declared_together() -> None:
    """CTRL-003 reconciles per account, so it needs to know which they are.

    With stock in three accounts, a control that reconciled only raw materials
    would silently leave two thirds of inventory uncovered (ADR-028) -- and it
    would still pass, because what it checks would still tie.
    """
    numbers = [account.number for account in accounts.INVENTORY_ACCOUNTS]
    assert numbers == ["130000", "135000", "137000"]
    for account in accounts.INVENTORY_ACCOUNTS:
        assert account.account_type == accounts.ASSET
        assert account.number in accounts.BY_NUMBER


def test_the_extract_matches_the_declarations() -> None:
    rows = accounts.rows()
    assert len(rows) == len(accounts.ACCOUNTS)
    spec_columns = set(accounts.EXTRACT_SPECS["chart_of_accounts"].columns)
    for row in rows:
        assert set(row) == spec_columns, (
            "the extract rows and the declared spec columns disagree, which the writer would reject"
        )


# ---------------------------------------------------------------------------
# The declaration against the postings
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def built() -> dataset.Dataset:
    data, _ = dataset.build(genconfig.load(), "demo")
    return data


def test_every_posted_account_is_declared(built: dataset.Dataset) -> None:
    """Postings to an undeclared account cannot be classified at all.

    Such a posting still balances its own document, so CTRL-002 passes -- it
    simply drops out of both the balance sheet and the income statement. That
    is why this is asserted here rather than left to the document check.
    """
    posted = {row["gl_account"] for row in built.materialise()["gl_postings"]}
    assert posted
    undeclared = sorted(posted - set(accounts.BY_NUMBER))
    assert not undeclared, f"posted to undeclared account(s): {undeclared}"


def test_the_chart_declares_no_account_nobody_posts_to(built: dataset.Dataset) -> None:
    """The other direction, and the one that rots quietly.

    An account declared and never used is a claim about the ledger that the
    ledger does not support. This currently permits the accounts ADR-028's
    chain is about to start using -- and names them, so the exemption cannot
    quietly become permanent.
    """
    posted = {row["gl_account"] for row in built.materialise()["gl_postings"]}

    # The standard-costing chain lands in the next commit (ADR-028). Until it
    # does, these three are declared and unposted on purpose.
    pending_the_chain = {
        accounts.WORK_IN_PROCESS.number,
        accounts.FINISHED_GOODS.number,
        accounts.PRODUCTION_VARIANCE.number,
        accounts.SCRAP_EXPENSE.number,
    }
    unused = sorted(set(accounts.BY_NUMBER) - posted - pending_the_chain)
    assert not unused, (
        f"account(s) declared but never posted to: {unused}. Either something "
        "posts there or the chart is overstating what the ledger contains."
    )
