"""The monthly trial balance (BTB-35, ADR-029).

Derived from the postings rather than generated beside them, so it reconciles
to the cent on a clean run -- the same choice as confirmations deriving from
the MES outcome and issues deriving from consumption. A control whose baseline
is noise cannot detect anything.

It arrives as a source extract rather than a warehouse model on purpose.
Computing it from the postings already loaded would only test our own
arithmetic: postings that balance per document must balance per period, so the
test could not fail for any reason a reviewer would care about.
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from generator import config as genconfig
from generator import dataset, trial_balance

D = decimal.Decimal


def _posting(account: str, date: dt.date, amount: str, indicator: str) -> dict:
    return {
        "gl_account": account,
        "posting_date": date,
        "fiscal_year": date.year,
        "amount_usd": D(amount),
        "debit_credit_indicator": indicator,
    }


def test_debits_and_credits_are_summed_separately() -> None:
    rows = trial_balance.build(
        [
            _posting("130000", dt.date(2026, 8, 5), "100.00", "S"),
            _posting("130000", dt.date(2026, 8, 9), "30.00", "H"),
        ],
        company_code="1000",
    )
    assert len(rows) == 1
    assert rows[0]["debits_usd"] == D("100.00")
    assert rows[0]["credits_usd"] == D("30.00")
    assert rows[0]["closing_balance_usd"] == D("70.00")
    assert rows[0]["opening_balance_usd"] == D("0.00")


def test_a_balance_carries_into_the_next_period() -> None:
    """The point of an opening balance.

    Without it each period starts from nothing, and a monthly trial balance is
    a sequence of snapshots rather than a statement about a going concern.
    """
    rows = trial_balance.build(
        [
            _posting("130000", dt.date(2026, 8, 5), "100.00", "S"),
            _posting("130000", dt.date(2026, 9, 5), "40.00", "S"),
        ],
        company_code="1000",
    )
    by_period = {r["posting_period"]: r for r in rows}
    assert by_period["2026-08"]["closing_balance_usd"] == D("100.00")
    assert by_period["2026-09"]["opening_balance_usd"] == D("100.00")
    assert by_period["2026-09"]["closing_balance_usd"] == D("140.00")


def test_an_account_appears_in_every_period_after_its_first() -> None:
    """A reader comparing two months needs the same accounts in both.

    An account that vanishes from a period looks like a closed account rather
    than a quiet one.
    """
    rows = trial_balance.build(
        [
            _posting("130000", dt.date(2026, 8, 5), "100.00", "S"),
            # September has to exist as a period for the carry-forward to be
            # visible. A period with no postings at all is not a period of the
            # ledger, which is why this posting is here rather than the test
            # asserting a month the data never had.
            _posting("191100", dt.date(2026, 9, 5), "10.00", "H"),
            _posting("510000", dt.date(2026, 10, 5), "25.00", "S"),
        ],
        company_code="1000",
    )
    periods = {
        r["gl_account"]: sorted(
            x["posting_period"] for x in rows if x["gl_account"] == r["gl_account"]
        )
        for r in rows
    }
    # 130000 posts in August, so it appears in all three.
    assert periods["130000"] == ["2026-08", "2026-09", "2026-10"]
    # 510000 first posts in October, so it does not appear before that: a zero
    # balance from before an account was ever used is noise, not information.
    assert periods["510000"] == ["2026-10"]


def test_a_quiet_period_carries_the_balance_with_no_movement() -> None:
    rows = trial_balance.build(
        [
            _posting("130000", dt.date(2026, 8, 5), "100.00", "S"),
            _posting("510000", dt.date(2026, 9, 5), "1.00", "S"),
        ],
        company_code="1000",
    )
    quiet = next(
        r for r in rows if r["gl_account"] == "130000" and r["posting_period"] == "2026-09"
    )
    assert quiet["debits_usd"] == D("0.00")
    assert quiet["credits_usd"] == D("0.00")
    assert quiet["opening_balance_usd"] == D("100.00")
    assert quiet["closing_balance_usd"] == D("100.00")


def test_an_unbalanced_trial_balance_raises_rather_than_being_emitted() -> None:
    """Checked in the generator, not left to the control.

    The control's job is to detect a disagreement between the trial balance and
    the postings. A trial balance that fails to balance against ITSELF is
    arithmetic, and emitting one would make the control fail for a reason that
    has nothing to do with the data it watches.
    """
    one_sided = [_posting("130000", dt.date(2026, 8, 5), "100.00", "S")]
    rows = trial_balance.build(one_sided, company_code="1000")
    with pytest.raises(trial_balance.UnbalancedTrialBalanceError) as raised:
        trial_balance.assert_balanced(rows)
    assert "2026-08" in str(raised.value)


def test_no_postings_produces_no_rows() -> None:
    assert trial_balance.build([], company_code="1000") == []


# ---------------------------------------------------------------------------
# Against the real ledger
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def built() -> dict[str, list[dict]]:
    data, _ = dataset.build(genconfig.load(), "demo")
    return data.materialise()


def test_every_period_nets_to_zero(built: dict) -> None:
    """CTRL-019's first check, asserted at generation too.

    The one assertion a trial balance exists to make. Compared to the cent: a
    tolerance would mask exactly the rounding faults the money guards in the
    writer and the loader exist to prevent.
    """
    totals = trial_balance.closing_by_period(built["trial_balance"])
    assert totals
    for period, total in sorted(totals.items()):
        assert total == 0, f"{period} closing balances sum to {total:,}"


def test_the_trial_balance_agrees_with_the_postings_per_account_and_period(
    built: dict,
) -> None:
    """CTRL-019's second check, and the reason the first is not enough.

    Two accounts wrong in opposite directions leave the period netting to zero,
    so the zero-sum check cannot see it. This compares each account's own
    movement.

    Compared on movement rather than on closing balance: an early difference
    would otherwise fail every period after it and hide where it started.
    """
    import collections

    ledger: dict[tuple[str, str], D] = collections.defaultdict(lambda: D(0))
    for row in built["gl_postings"]:
        key = (row["gl_account"], row["posting_date"].strftime("%Y-%m"))
        ledger[key] += row["signed_amount_usd"]

    balances: dict[tuple[str, str], D] = collections.defaultdict(lambda: D(0))
    for row in built["trial_balance"]:
        key = (row["gl_account"], row["posting_period"])
        balances[key] += row["closing_balance_usd"] - row["opening_balance_usd"]

    # Compared over the union with a default of zero, which is what CTRL-019's
    # full outer join does. The key sets legitimately differ: an account that
    # posts once keeps appearing in later periods with its balance carried
    # forward and zero movement, and the ledger has no rows for those periods.
    # An earlier version of this test asserted the key sets were equal and
    # failed on exactly that -- being stricter than the control it mirrors,
    # and wrong.
    for key in sorted(set(balances) | set(ledger)):
        assert balances[key] == ledger[key], (
            f"account {key[0]} period {key[1]}: trial balance {balances[key]:,} "
            f"against ledger {ledger[key]:,}"
        )

    # And every difference the key sets DO show must be a zero-movement
    # carry-forward, not a missing posting.
    for key in sorted(set(balances) - set(ledger)):
        assert balances[key] == 0, (
            f"account {key[0]} period {key[1]} has trial balance movement "
            f"{balances[key]:,} with no postings behind it"
        )


def test_every_account_in_the_trial_balance_is_in_the_chart(built: dict) -> None:
    from generator import accounts

    appearing = {row["gl_account"] for row in built["trial_balance"]}
    assert appearing
    undeclared = sorted(appearing - set(accounts.BY_NUMBER))
    assert not undeclared, f"trial balance names undeclared account(s): {undeclared}"


def test_the_balance_sheet_accounts_carry_the_closing_position(built: dict) -> None:
    """Assets positive, liabilities and equity negative, signed in the debit
    direction.

    Not decoration: it is the check that the sign convention survived the whole
    chain. An inverted sign anywhere would still net to zero overall.
    """
    from generator import accounts

    final_period = max(row["posting_period"] for row in built["trial_balance"])
    closing = {
        row["gl_account"]: row["closing_balance_usd"]
        for row in built["trial_balance"]
        if row["posting_period"] == final_period
    }

    for number in (accounts.INVENTORY_RAW.number, accounts.FINISHED_GOODS.number):
        assert closing.get(number, D(0)) > 0, f"asset account {number} does not close positive"
    for number in (accounts.GR_IR_CLEARING.number, accounts.OPENING_BALANCE.number):
        assert closing.get(number, D(0)) < 0, (
            f"account {number} is a liability or equity and does not close negative"
        )
