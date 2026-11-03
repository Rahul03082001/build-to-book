"""The chart of accounts (BTB-35).

Every account the generator posts to, declared in one place with its type and
its normal balance. Account numbers used to be four module constants in
`inventory.py` with the type carried only in a trailing comment, which meant
nothing could ask whether a posting was to an asset or an expense -- so a
balance sheet and an income statement could not be separated, and
`debit_credit_indicator` could not be checked against what the account
normally does.

Numbering follows the usual ledger blocks, which is how an analyst recognises
an account's nature before reading its name:

    1xxxxx  assets
    19xxxx  liabilities (the GR/IR clearing account sits here, as in SAP)
    2xxxxx  price differences
    3xxxxx  equity
    5xxxxx  expenses

The chain ADR-028 describes needs three inventory accounts rather than one.
That is where a manufacturer's money actually sits: components bought but not
yet issued, work in process on the floor, and finished product waiting to ship.
Collapsing them into a single inventory account -- which is what this project
did until now -- hides the two that production actually moves through.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from generator.writers import ExtractSpec

#: An account's normal balance: the side that increases it.
#:
#: Declared so a posting can be checked against it. An asset credited more than
#: it is debited over the life of a period is not necessarily wrong, but an
#: account whose balance sits permanently on the wrong side usually is.
DEBIT = "DEBIT"
CREDIT = "CREDIT"

ASSET = "ASSET"
LIABILITY = "LIABILITY"
EQUITY = "EQUITY"
EXPENSE = "EXPENSE"

#: Account types that belong on the balance sheet rather than the income
#: statement. A trial balance needs both, and needs to know which is which.
BALANCE_SHEET_TYPES = frozenset({ASSET, LIABILITY, EQUITY})


@dataclasses.dataclass(frozen=True)
class Account:
    """One general ledger account."""

    number: str
    name: str
    account_type: str
    normal_balance: str
    #: What posts here, in the words a reviewer would use. Ends up as the
    #: column description in the warehouse, so the data dictionary explains
    #: the account rather than restating its name.
    purpose: str

    @property
    def is_balance_sheet(self) -> bool:
        return self.account_type in BALANCE_SHEET_TYPES


#: Raw materials bought and not yet issued to production.
INVENTORY_RAW = Account(
    "130000",
    "Inventory - raw materials",
    ASSET,
    DEBIT,
    "Components received from suppliers, until they are issued to an order.",
)

#: Work in process. Components issued to an order land here and are relieved
#: when the order confirms, scraps, or closes with a variance (ADR-028).
WORK_IN_PROCESS = Account(
    "135000",
    "Work in process",
    ASSET,
    DEBIT,
    "Components issued to production orders, until the order confirms output, "
    "scraps a unit, or closes and its residual becomes a variance.",
)

#: Finished goods: completed racks, until dispatched.
FINISHED_GOODS = Account(
    "137000",
    "Finished goods",
    ASSET,
    DEBIT,
    "Racks confirmed complete at standard cost, until they are dispatched.",
)

#: Goods received, invoice not yet received. The liability between a receipt
#: and the supplier invoice that pays for it -- the third leg of the three-way
#: match lives against this account.
GR_IR_CLEARING = Account(
    "191100",
    "Goods received / invoice received",
    LIABILITY,
    CREDIT,
    "Owed to suppliers for goods received but not yet invoiced.",
)

#: Standard cost revaluation (ADR-023). A value-only entry when a standard cost
#: changes: nothing moved physically, so it is a price difference, not a
#: consumption.
PRICE_DIFFERENCE = Account(
    "231000",
    "Standard cost price difference",
    EXPENSE,
    DEBIT,
    "Revaluation of on-hand stock when a standard cost changes (ADR-023).",
)

#: Offsets opening stock. A real opening balance comes from the prior period's
#: closing balance sheet; with no prior period in the data, the counter-entry
#: goes here so the opening trial balance still nets to zero (ADR-027).
OPENING_BALANCE = Account(
    "399000",
    "Retained earnings / opening balance",
    EQUITY,
    CREDIT,
    "Counter-entry for opening stock, which has no prior period to come from.",
)

#: Cost of goods sold, recognised at dispatch (ADR-028).
COST_OF_GOODS_SOLD = Account(
    "510000",
    "Cost of goods sold",
    EXPENSE,
    DEBIT,
    "Standard cost of finished goods dispatched. Recognised at dispatch, not "
    "at component issue (ADR-028).",
)

#: Production variance: the work-in-process residual when an order closes.
PRODUCTION_VARIANCE = Account(
    "520000",
    "Production variance",
    EXPENSE,
    DEBIT,
    "What an order absorbed in components less what it relieved as output and "
    "scrap. Debit means it consumed more than standard.",
)

#: Scrapped units, written off out of work in process.
SCRAP_EXPENSE = Account(
    "530000",
    "Scrap expense",
    EXPENSE,
    DEBIT,
    "Standard cost of units scrapped after exhausting their rework attempts.",
)

#: Every account, in number order. The order is the output order, so the
#: extract reads like a chart of accounts rather than like a dict.
ACCOUNTS: tuple[Account, ...] = (
    INVENTORY_RAW,
    WORK_IN_PROCESS,
    FINISHED_GOODS,
    GR_IR_CLEARING,
    PRICE_DIFFERENCE,
    OPENING_BALANCE,
    COST_OF_GOODS_SOLD,
    PRODUCTION_VARIANCE,
    SCRAP_EXPENSE,
)

BY_NUMBER: dict[str, Account] = {account.number: account for account in ACCOUNTS}

#: The accounts that hold inventory. CTRL-003 reconciles the subledger to the
#: general ledger, and with stock in three accounts it has to do so per account
#: -- reconciling only raw materials would silently leave two thirds of
#: inventory uncovered (ADR-028).
INVENTORY_ACCOUNTS: tuple[Account, ...] = (INVENTORY_RAW, WORK_IN_PROCESS, FINISHED_GOODS)

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "chart_of_accounts": ExtractSpec(
        name="chart_of_accounts",
        columns=[
            "gl_account",
            "account_name",
            "account_type",
            "normal_balance",
            "is_balance_sheet",
            "account_purpose",
        ],
        source_system="FINANCE",
        # Master data carries no amount. Completeness rests on the row count.
        value_column=None,
        natural_key=["gl_account"],
    ),
}


def rows() -> list[dict[str, Any]]:
    """The chart of accounts as extract rows.

    Static: a chart of accounts is master data maintained by finance, not
    something a run invents. Generated from the declarations above so the
    extract and the accounts the generator posts to cannot disagree -- a test
    asserts every account a posting names appears here.
    """
    return [
        {
            "gl_account": account.number,
            "account_name": account.name,
            "account_type": account.account_type,
            "normal_balance": account.normal_balance,
            "is_balance_sheet": account.is_balance_sheet,
            "account_purpose": account.purpose,
        }
        for account in ACCOUNTS
    ]
