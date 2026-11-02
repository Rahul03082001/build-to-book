"""Defect injectors for Phase 1 (BTB-26, BTB-27).

Each injector mutates the generated data in place and returns the records it
planted, so `defects_manifest.json` lists exactly what was done. That manifest
is what makes a detection test meaningful: without it a test can only assert
that a control flagged *something*, which says nothing about whether it caught
everything.

Injectors are deliberately small and surgical. A defect that corrupts a lot of
data proves little -- the interesting question is whether a control notices one
bad row among fifteen thousand good ones.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
from typing import Any

from generator.defects import InjectedRecord, register
from generator.inventory import (
    ACCOUNT_CONSUMPTION,
    ACCOUNT_GR_IR_CLEARING,
)
from generator.writers import money


def money_like(rng: Any) -> Any:
    """A plausible adjustment amount, to two decimal places."""
    return money(decimal.Decimal(int(rng.integers(5_000, 250_000))))


@register("duplicate_goods_receipt")
def inject_duplicate_goods_receipt(
    goods_movements: list[dict[str, Any]],
    gl_postings: list[dict[str, Any]],
    count: int,
    rng: Any,
) -> list[InjectedRecord]:
    """Repeat whole goods receipts in the extract, as an interface retry does.

    A retry re-sends the same material document, so the extract contains the
    same document line twice -- identical natural key and all. That is what
    makes it *preventive*: the loader can refuse it at the door rather than
    discovering overstated inventory later (ADR-020).

    The matching GL lines are duplicated too. A retry replays the whole
    document, not just its material movement, and duplicating only one side
    would make the subledger-to-GL reconciliation fail for a reason the real
    fault would not cause.

    Modelled on a duplicate *within one file*. The other shape -- a retry that
    allocates a fresh document number for the same physical receipt -- is a
    business-level duplicate that loads cleanly and needs a three-way-match
    control to find. That is a different defect and belongs with Phase 2's
    `receipt_without_po` family.
    """
    receipts = [
        movement
        for movement in goods_movements
        if movement["movement_type"] == "101" and movement["material_document_item"] == 1
    ]
    if not receipts:
        return []

    chosen = [
        receipts[int(i)]
        for i in rng.choice(len(receipts), size=min(count, len(receipts)), replace=False)
    ]

    planted: list[InjectedRecord] = []
    for movement in chosen:
        document = movement["material_document"]
        year = movement["fiscal_year"]

        # Every line of the document, so the duplicate is a whole document.
        lines = [
            m
            for m in goods_movements
            if m["material_document"] == document and m["fiscal_year"] == year
        ]
        for line in lines:
            goods_movements.append(copy.deepcopy(line))
            planted.append(
                InjectedRecord(
                    defect_key="duplicate_goods_receipt",
                    extract="erp_goods_movements",
                    natural_key={
                        "material_document": line["material_document"],
                        "fiscal_year": line["fiscal_year"],
                        "material_document_item": line["material_document_item"],
                    },
                    note="interface retry: the material document line appears twice in the extract",
                )
            )

        postings = [p for p in gl_postings if p["reference_document"] == document]
        for posting in postings:
            gl_postings.append(copy.deepcopy(posting))
            planted.append(
                InjectedRecord(
                    defect_key="duplicate_goods_receipt",
                    extract="finance_gl_postings",
                    natural_key={
                        "accounting_document": posting["accounting_document"],
                        "fiscal_year": posting["fiscal_year"],
                        "line_item": posting["line_item"],
                    },
                    note="interface retry: the accounting document line appears twice in the extract",
                )
            )

    return planted


@register("backdated_gl_posting")
def inject_backdated_gl_posting(
    gl_postings: list[dict[str, Any]],
    count: int,
    close_date_for: Any,
    next_document: Any,
    rng: Any,
) -> list[InjectedRecord]:
    """Enter a NEW accounting document into a period that had already closed.

    A backdated posting is a document created *now* carrying an old posting
    date -- an accrual or a cost reclassification squeezed into a month that
    was already reported. So the injector appends a document rather than
    editing one.

    An earlier version mutated `entered_on` on an existing document, and the
    defect never reached the warehouse: the loader's anti-join inserts only
    unseen natural keys (ADR-020), so a changed value on a row already in
    `raw` is skipped. Four records were planted and zero were detectable. That
    is a real property of insert-only loading, not a bug in the injector, and
    it is why this defect had to become an insert.

    The document is deliberately **balanced** and posts to consumption and
    GR/IR clearing rather than to inventory. Both choices keep the other
    controls quiet so CTRL-006 is demonstrably the one that catches it: an
    unbalanced document would trip the double-entry check, and touching
    account 130000 would break the subledger-to-GL tie. Neither would be
    wrong -- a real backdated entry often does break several things at once --
    but then the test would not show that CTRL-006 works.

    It references a real material document, so the posting still traces to a
    source document and the lineage test stays satisfied.
    """
    periods = sorted({p["posting_date"].strftime("%Y-%m") for p in gl_postings})
    if len(periods) < 2:
        return []
    # The earliest period is certainly closed; a later one may not be.
    target_period = periods[0]

    in_period = [p for p in gl_postings if p["posting_date"].strftime("%Y-%m") == target_period]
    if not in_period:
        return []

    # A real document from the period, to reference and to copy the shape of.
    template = in_period[int(rng.integers(0, len(in_period)))]
    posting_date = template["posting_date"]
    fiscal_year = template["fiscal_year"]
    closed_on = close_date_for(posting_date)

    planted: list[InjectedRecord] = []
    for _ in range(count):
        document = next_document(fiscal_year)
        entered_on = closed_on + dt.timedelta(days=int(rng.integers(10, 40)))
        amount = money_like(rng)

        pairs = [
            (ACCOUNT_CONSUMPTION, "S", amount),
            (ACCOUNT_GR_IR_CLEARING, "H", -amount),
        ]
        for line_item, (account, indicator, signed) in enumerate(pairs, start=1):
            gl_postings.append(
                {
                    "accounting_document": document,
                    "line_item": line_item,
                    "fiscal_year": fiscal_year,
                    "company_code": template["company_code"],
                    "gl_account": account,
                    "debit_credit_indicator": indicator,
                    "amount_usd": abs(signed),
                    "signed_amount_usd": signed,
                    "currency": template["currency"],
                    "posting_date": posting_date,
                    "document_date": posting_date,
                    # The tell: entered long after the period locked.
                    "entered_on": entered_on,
                    "reference_document": template["reference_document"],
                    "material_number": None,
                    "entered_by": "FIN.ADJ",
                }
            )
            planted.append(
                InjectedRecord(
                    defect_key="backdated_gl_posting",
                    extract="finance_gl_postings",
                    natural_key={
                        "accounting_document": document,
                        "fiscal_year": fiscal_year,
                        "line_item": line_item,
                    },
                    note=(
                        f"entered {entered_on.isoformat()}, after period "
                        f"{target_period} closed on {closed_on.isoformat()}"
                    ),
                )
            )

    return planted
