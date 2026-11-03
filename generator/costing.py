"""Standard cost roll-up for manufactured materials (BTB-35).

A bought component's standard cost is a price somebody negotiated. A
manufactured one is **not independent**: it is the sum of what goes into it.
That is what a standard-costing system rolls up, and it is the number finished
goods are valued at on the balance sheet.

This project did not do it. Rack and tray standard costs were drawn from the
same price band as components, with no reference to their bills of materials,
and the gap was not subtle:

    rack 1000008   declared standard      8,461
                   rolled up from BOM 1,161,502     137x

It did not matter while racks never entered inventory -- production was
invisible to finance, so nothing was ever valued at a rack's standard cost.
ADR-028 puts finished goods on the balance sheet, and then it matters twice
over: the general ledger would carry one figure while CTRL-003's quantity times
standard cost invariant computed another, and the production variance would be
the difference between a real cost and an arbitrary one.

The roll-up is bottom-up, because a rack contains trays that contain
components. Depth is bounded by the same guard the BOM explosion uses: a cycle
would otherwise recurse until the stack gave out, and `assert_bom_has_no_cycles`
asserts acyclicity against the raw data rather than trusting the generator.

Manufactured materials get a single cost window covering the whole period. A
standard is set at a period boundary and revised deliberately, not recalculated
whenever a component's price moves -- which is also what leaves standard cost
revaluation (ADR-023) something real to do.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
from collections.abc import Sequence
from typing import Any

D = decimal.Decimal

#: Open-ended window sentinel, SAP's convention (ADR-002, ADR-007).
OPEN_ENDED = dt.date(9999, 12, 31)

#: Depth bound on the roll-up recursion, matching `max_bom_depth` in
#: dbt_project.yml. A cyclic bill of materials fails fast and visibly instead of
#: exhausting the stack.
MAX_DEPTH = 10


class BomCycleError(Exception):
    """A bill of materials contains itself, directly or through a level."""


def _money(value: D) -> D:
    return value.quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP)


def roll_up(
    bom_headers: Sequence[dict[str, Any]],
    bom_items: Sequence[dict[str, Any]],
    component_cost: dict[str, D],
) -> dict[str, D]:
    """Standard cost of every manufactured material, from its components.

    Args:
        bom_headers: BOM headers, giving the parent material per BOM.
        bom_items: BOM items, giving each component and its quantity per
            parent.
        component_cost: Standard cost of every material as drawn for the
            master, used for the leaves.

    Returns:
        Manufactured material -> rolled-up standard cost. Materials with no
        bill of materials are absent: their cost is a negotiated price, not a
        sum.

    Raises:
        BomCycleError: if the structure contains a cycle.
    """
    parent_of_bom = {h["bom_number"]: h["material_number"] for h in bom_headers}
    children: dict[str, list[tuple[str, D]]] = {}
    for item in bom_items:
        parent = parent_of_bom.get(item["bom_number"])
        if parent is None:
            # An item whose header does not exist. Left out rather than
            # guessed at: the dbt source tests catch an orphan BOM item, and
            # inventing a parent here would hide it.
            continue
        children.setdefault(parent, []).append(
            (item["component_material_number"], D(str(item["quantity_per"])))
        )

    rolled: dict[str, D] = {}

    def cost_of(material: str, path: tuple[str, ...]) -> D:
        if material in path:
            raise BomCycleError(
                f"bill of materials contains a cycle: {' -> '.join((*path, material))}"
            )
        if len(path) > MAX_DEPTH:
            raise BomCycleError(
                f"bill of materials deeper than {MAX_DEPTH} levels at {material}; "
                "either the structure is wrong or the bound needs raising"
            )
        if material in rolled:
            return rolled[material]
        if material not in children:
            # A leaf: a bought part, valued at the price in the master.
            return component_cost.get(material, D(0))

        total = D(0)
        for component, quantity in children[material]:
            total += cost_of(component, (*path, material)) * quantity
        rolled[material] = _money(total)
        return rolled[material]

    for material in sorted(children):
        cost_of(material, ())
    return rolled


def apply_to_standard_costs(
    standard_costs: list[dict[str, Any]],
    rolled: dict[str, D],
    cost_lookup: dict[str, list[tuple[dt.date, dt.date, D]]],
    period_start: dt.date,
    currency: str,
    approved_by: str,
) -> int:
    """Replace the drawn cost of every manufactured material with its roll-up.

    Mutates `standard_costs` and `cost_lookup` in place so every later
    valuation -- goods movements, GL postings, the inventory balance -- uses the
    same number. Two sources of truth for a standard cost is how the ledger and
    the quantity-times-cost invariant come to disagree.

    A manufactured material gets ONE window covering the whole period,
    replacing however many the master drew for it. A standard is set at a
    period boundary, not recalculated whenever a component's price moves.

    Returns:
        How many materials were revalued.
    """
    if not rolled:
        return 0

    kept = [row for row in standard_costs if row["material_number"] not in rolled]
    replacements = [
        {
            "material_number": material,
            "valid_from": period_start,
            "valid_to": OPEN_ENDED,
            "standard_cost_usd": cost,
            "currency": currency,
            "approved_by": approved_by,
            # Approved before the window opens, as a standard is.
            "approved_on": period_start - dt.timedelta(days=7),
        }
        for material, cost in sorted(rolled.items())
    ]

    standard_costs[:] = sorted(
        kept + replacements, key=lambda row: (row["material_number"], row["valid_from"])
    )
    for material, cost in rolled.items():
        cost_lookup[material] = [(period_start, OPEN_ENDED, cost)]
    return len(rolled)


# ---------------------------------------------------------------------------
# The standard-costing chain (ADR-028)
#
#   issue      261   Cr raw materials      Dr work in process
#   confirm    101   Cr work in process    Dr finished goods
#   scrap            Cr work in process    Dr scrap expense
#   residual         Cr/Dr work in process <-> production variance
#   dispatch   601   Cr finished goods     Dr cost of goods sold
#
# Before this, production was invisible to finance: the factory completed 1,199
# units in the demo window and not one of them touched the books. A balance
# sheet built from it showed components bought and expensed, and no product
# anywhere.
# ---------------------------------------------------------------------------

#: Movement type for a goods receipt from a production order.
#:
#: 101, the same type as a purchase receipt, because that is what SAP uses --
#: the two are distinguished by the reference, not the type. A receipt carries
#: either a purchase order or a production order, never both, so anything
#: counting purchases keys on `po_number` and is unaffected. A test asserts
#: that separation holds.
MVT_PRODUCTION_RECEIPT = "101"

#: Goods issue for delivery, SAP's 601. Relieves finished goods and recognises
#: cost of sale (ADR-028). There is no customer master and no revenue in this
#: project, so a dispatch is the whole of the sales side -- stated plainly in
#: ADR-028 rather than left as an apparent omission.
MVT_DISPATCH = "601"


@dataclasses.dataclass
class OrderCosting:
    """What one production order absorbed and relieved, at standard.

    `variance` is the work-in-process residual: what the order took in as
    components less what it gave out as finished goods and scrap. Zero would
    mean the order consumed exactly its bill of materials at exactly the cost
    the roll-up used -- which is nearly true by construction, and differs where
    a component's standard cost changed mid-period while the rolled-up cost was
    set at period start. That difference is a real price variance, and it is
    the reason the account exists rather than being decoration.
    """

    order_number: str
    posting_date: dt.date
    absorbed: D
    finished: D
    scrapped: D

    @property
    def variance(self) -> D:
        return _money(self.absorbed - self.finished - self.scrapped)


def production_movements(
    confirmations: Sequence[dict[str, Any]],
    order_plan: dict[str, dict[str, Any]],
    unit_cost: dict[str, D],
    next_document: Any,
    clerk_for: Any,
    dispatch_rate: float,
    period_end: dt.date,
    rng: Any,
    plant: str,
    uom: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Finished goods receipts and dispatches, as material movements.

    Finished goods have to move physically, not only in the ledger. A dispatch
    relieves stock, so without a receipt to match it the finished goods balance
    would go straight negative and CTRL-010 would be right to fail -- which is
    how this was caught in design rather than in data.

    Returns:
        (receipts, dispatches), each a list of movement rows.
    """
    receipts: list[dict[str, Any]] = []
    dispatches: list[dict[str, Any]] = []

    for confirmation in sorted(confirmations, key=lambda c: c["order_number"]):
        order_number = confirmation["order_number"]
        plan = order_plan.get(order_number)
        if plan is None:
            continue
        material = plan["material_number"]
        cost = unit_cost.get(material)
        if cost is None or cost <= 0:
            # A rack with no rolled-up cost would value its output at nothing,
            # so finished goods would carry quantity and no value and CTRL-003's
            # quantity-times-cost invariant would fail. Skipped loudly by the
            # test that asserts every confirmed order produces a receipt.
            continue

        confirmed = int(confirmation["confirmed_quantity"])
        if confirmed <= 0:
            continue

        posting = confirmation["posting_date"]
        receipts.append(
            {
                "material_document": next_document(posting.year),
                # One item per document: a production receipt is a single line.
                # Raw-material documents get their item number during assembly,
                # where several receipts for one purchase order share a
                # document -- these do not.
                "material_document_item": 1,
                "fiscal_year": posting.year,
                "movement_type": MVT_PRODUCTION_RECEIPT,
                "material_number": material,
                "plant": plant,
                "quantity": confirmed,
                "uom": uom,
                "amount_usd": _money(cost * confirmed),
                "posting_date": posting,
                "document_date": posting,
                "po_number": None,
                "po_item": None,
                "order_number": order_number,
                "entered_by": clerk_for(),
                "inbound_delivery": None,
            }
        )

        # A share is dispatched; the rest stays in finished goods, because a
        # manufacturer ships from stock rather than emptying it. Dispatching
        # everything would leave finished goods at zero every period, which is
        # the same fault that made closing inventory zero before
        # `inventory_retention` existed.
        #
        # The fraction is carried stochastically rather than truncated. Orders
        # are small -- about three units on the demo profile -- so
        # int(3 * 0.85) = 2 dispatches 67% while the configuration says 85%,
        # and the declared rate would simply not be true. Rounding is no better:
        # round(3 * 0.85) = 3 dispatches everything. Taking the floor and adding
        # one with probability equal to the remainder makes the AGGREGATE match
        # the rate, which is what the configuration is a statement about.
        exact = confirmed * dispatch_rate
        shipped = int(exact)
        if rng.random() < exact - shipped:
            shipped += 1
        shipped = min(shipped, confirmed)
        if shipped <= 0:
            continue
        # Dispatched after it was built, and inside the window.
        earliest = posting + dt.timedelta(days=1)
        if earliest > period_end:
            continue
        span = (period_end - earliest).days
        dispatch_date = earliest + dt.timedelta(
            days=int(rng.integers(0, span + 1)) if span > 0 else 0
        )
        dispatches.append(
            {
                "material_document": next_document(dispatch_date.year),
                "material_document_item": 1,
                "fiscal_year": dispatch_date.year,
                "movement_type": MVT_DISPATCH,
                "material_number": material,
                "plant": plant,
                # Negative: stock leaves. Signed at source by movement type,
                # as receipts and issues already are.
                "quantity": -shipped,
                "uom": uom,
                "amount_usd": _money(-cost * shipped),
                "posting_date": dispatch_date,
                "document_date": dispatch_date,
                "po_number": None,
                "po_item": None,
                "order_number": order_number,
                "entered_by": clerk_for(),
                "inbound_delivery": None,
            }
        )

    return receipts, dispatches


def order_costings(
    confirmations: Sequence[dict[str, Any]],
    order_plan: dict[str, dict[str, Any]],
    unit_cost: dict[str, D],
    issue_value_by_order: dict[str, D],
) -> list[OrderCosting]:
    """What each order absorbed and relieved, for the scrap and variance entries.

    Absorbed comes from the issues actually posted, not from the bill of
    materials: the point of a variance is to compare what happened against the
    standard, so taking both sides from the standard would guarantee zero.
    """
    costings: list[OrderCosting] = []
    for confirmation in sorted(confirmations, key=lambda c: c["order_number"]):
        order_number = confirmation["order_number"]
        plan = order_plan.get(order_number)
        if plan is None:
            continue
        cost = unit_cost.get(plan["material_number"], D(0))
        costings.append(
            OrderCosting(
                order_number=order_number,
                posting_date=confirmation["posting_date"],
                absorbed=_money(issue_value_by_order.get(order_number, D(0))),
                finished=_money(cost * int(confirmation["confirmed_quantity"])),
                scrapped=_money(cost * int(confirmation["scrap_quantity"])),
            )
        )
    return costings
