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
