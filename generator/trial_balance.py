"""The monthly trial balance (BTB-35, ADR-029).

One row per account per period, with opening balance, debits, credits and
closing balance. Derived from the postings rather than generated beside them,
so it reconciles to the cent on a clean run -- the same choice as production
confirmations deriving from the MES outcome (BTB-33) and goods issues deriving
from consumption (ADR-027). A control whose baseline is noise cannot detect
anything.

It arrives as a **source extract** rather than a warehouse model. Computing it
from the postings already loaded would only test our own arithmetic: postings
that balance per document must balance per period, so the test could not fail
for any reason a reviewer would care about. As an extract it creates a real
cross-system reconciliation -- "the trial balance finance published agrees with
the postings they sent us" -- and it backs the `finance_trial_balance`
freshness window that config.yaml has declared since Phase 0 with nothing
behind it.

Opening balances matter more than they look. Without them the first period
starts from nothing, and the opening stock entry (ADR-027) would be the only
thing standing in for a business that existed before the window. With them,
each period's closing balance carries into the next, which is what makes a
monthly trial balance a statement about a going concern rather than a
three-month snapshot.
"""

from __future__ import annotations

import collections
import datetime as dt
import decimal
from collections.abc import Sequence
from typing import Any

from generator.writers import ExtractSpec

D = decimal.Decimal

#: SAP debit/credit indicators: S debits, H credits.
DEBIT_INDICATOR = "S"

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "trial_balance": ExtractSpec(
        name="trial_balance",
        columns=[
            "company_code",
            "fiscal_year",
            "posting_period",
            "gl_account",
            "opening_balance_usd",
            "debits_usd",
            "credits_usd",
            "closing_balance_usd",
        ],
        source_system="FINANCE",
        # The control total is the closing balance. Summed across every row of
        # a period it must be zero, which is the one number a trial balance
        # exists to assert -- so it is the figure the sidecar carries.
        value_column="closing_balance_usd",
        natural_key=["company_code", "fiscal_year", "posting_period", "gl_account"],
    ),
}


def build(
    postings: Sequence[dict[str, Any]],
    company_code: str,
) -> list[dict[str, Any]]:
    """One row per account per period, with balances carried forward.

    Args:
        postings: Every GL posting line. Both sides of every document, because
            a trial balance is the whole ledger rather than a selection of it.
        company_code: The company the balances belong to.

    Returns:
        Rows in (period, account) order, which is how a trial balance is read.

    Every account that posts in any period gets a row in **every** period from
    its first appearance onward, even where it had no activity. A reader
    comparing two months needs the same accounts in both, and an account that
    vanishes from a period looks like a closed account rather than a quiet one.
    """
    if not postings:
        return []

    movement: dict[tuple[str, str], list[D]] = collections.defaultdict(lambda: [D(0), D(0)])
    periods: set[str] = set()
    year_of_period: dict[str, int] = {}

    for row in postings:
        period = _period(row["posting_date"])
        account = str(row["gl_account"])
        periods.add(period)
        year_of_period[period] = int(row["fiscal_year"])

        amount = abs(D(str(row["amount_usd"])))
        side = 0 if row["debit_credit_indicator"] == DEBIT_INDICATOR else 1
        movement[account, period][side] += amount

    accounts_seen = sorted({account for account, _ in movement})
    ordered_periods = sorted(periods)

    rows: list[dict[str, Any]] = []
    # Carried forward per account, which is the whole point of an opening
    # balance: period two opens where period one closed.
    closing: dict[str, D] = dict.fromkeys(accounts_seen, D(0))
    first_period: dict[str, str] = {}
    for account in accounts_seen:
        for period in ordered_periods:
            if (account, period) in movement:
                first_period[account] = period
                break

    for period in ordered_periods:
        for account in accounts_seen:
            # No row before the account's first activity: a balance of zero
            # from before an account was ever used is noise, not information.
            if first_period.get(account, period) > period:
                continue

            debits, credits = movement.get((account, period), [D(0), D(0)])
            opening = closing[account]
            # Signed in the debit direction throughout, so a period's net
            # movement is a plain sum and the whole ledger nets to zero. The
            # alternative -- storing each account on its own natural side --
            # means every consumer has to know each account's sign convention.
            period_closing = _money(opening + debits - credits)
            closing[account] = period_closing

            rows.append(
                {
                    "company_code": company_code,
                    "fiscal_year": year_of_period[period],
                    "posting_period": period,
                    "gl_account": account,
                    "opening_balance_usd": _money(opening),
                    "debits_usd": _money(debits),
                    "credits_usd": _money(credits),
                    "closing_balance_usd": period_closing,
                }
            )

    return rows


def _period(value: dt.date) -> str:
    return value.strftime("%Y-%m")


def _money(value: D) -> D:
    return value.quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP)


def closing_by_period(rows: Sequence[dict[str, Any]]) -> dict[str, D]:
    """Sum of closing balances per period. Must be zero in every period.

    Here so the generator can assert it before anything is written: a trial
    balance that does not balance is not a finding to detect downstream, it is
    a fault in how it was built.
    """
    totals: dict[str, D] = collections.defaultdict(lambda: D(0))
    for row in rows:
        totals[row["posting_period"]] += D(str(row["closing_balance_usd"]))
    return dict(totals)


class UnbalancedTrialBalanceError(Exception):
    """A period's closing balances do not net to zero."""


def assert_balanced(rows: Sequence[dict[str, Any]]) -> None:
    """Refuse to emit a trial balance that does not balance.

    Checked here rather than left to the control, because the control's job is
    to detect a disagreement between the trial balance and the postings. A
    trial balance that fails to balance against ITSELF is arithmetic, and
    emitting one would make the control fail for a reason that has nothing to
    do with the data it is meant to be watching.
    """
    unbalanced = {period: total for period, total in closing_by_period(rows).items() if total != 0}
    if unbalanced:
        raise UnbalancedTrialBalanceError(
            "period(s) whose closing balances do not net to zero: "
            + ", ".join(f"{period} = {total:,}" for period, total in sorted(unbalanced.items()))
        )
