"""Standard cost roll-up for manufactured materials (BTB-35).

A bought component's standard cost is a negotiated price. A manufactured one is
the sum of what goes into it. This project drew both from the same price band,
and rack 1000008 carried 8,461 against a bill of materials worth 1,163,783 --
137 times out.

It was harmless while production was invisible to finance, because nothing was
ever valued at a rack's standard cost. ADR-028 puts finished goods on the
balance sheet, and then the general ledger and CTRL-003's quantity-times-cost
invariant would compute inventory from two different numbers.
"""

from __future__ import annotations

import datetime as dt
import decimal

import pytest

from generator import costing

D = decimal.Decimal


def _bom(parent: str, components: list[tuple[str, str]], number: str) -> tuple[dict, list[dict]]:
    header = {"bom_number": number, "material_number": parent}
    items = [
        {"bom_number": number, "component_material_number": c, "quantity_per": q}
        for c, q in components
    ]
    return header, items


def test_a_single_level_rolls_up_to_the_sum_of_its_components() -> None:
    header, items = _bom("TRAY", [("GPU", "4"), ("CPU", "2")], "1")
    rolled = costing.roll_up([header], items, {"GPU": D("1000.00"), "CPU": D("500.00")})
    assert rolled["TRAY"] == D("5000.00")


def test_a_multi_level_structure_rolls_up_through_its_levels() -> None:
    """A rack contains trays that contain components.

    The roll-up has to be bottom-up. Taking the drawn cost of a tray instead of
    its rolled-up cost is the whole fault being fixed, one level down.
    """
    tray_header, tray_items = _bom("TRAY", [("GPU", "4")], "1")
    rack_header, rack_items = _bom("RACK", [("TRAY", "3"), ("CABLE", "10")], "2")
    rolled = costing.roll_up(
        [tray_header, rack_header],
        tray_items + rack_items,
        {"GPU": D("1000.00"), "CABLE": D("20.00")},
    )
    assert rolled["TRAY"] == D("4000.00")
    # 3 trays at 4,000 plus 10 cables at 20.
    assert rolled["RACK"] == D("12200.00")


def test_a_bought_component_is_not_rolled_up() -> None:
    """Its cost is a price somebody negotiated, not a sum."""
    header, items = _bom("TRAY", [("GPU", "4")], "1")
    rolled = costing.roll_up([header], items, {"GPU": D("1000.00")})
    assert "GPU" not in rolled


def test_a_cycle_raises_rather_than_recursing() -> None:
    """A bill of materials containing itself would exhaust the stack.

    `assert_bom_has_no_cycles` asserts acyclicity against the raw data rather
    than trusting the generator; this is the same guard on the Python side, so
    a cyclic structure fails with a named path instead of a RecursionError.
    """
    a_header, a_items = _bom("A", [("B", "1")], "1")
    b_header, b_items = _bom("B", [("A", "1")], "2")
    with pytest.raises(costing.BomCycleError) as raised:
        costing.roll_up([a_header, b_header], a_items + b_items, {})
    assert "cycle" in str(raised.value)
    assert "A" in str(raised.value)


def test_an_item_whose_header_is_missing_is_left_out() -> None:
    """Rather than guessed at.

    An orphan BOM item is caught by the source tests. Inventing a parent for it
    here would hide it and silently add cost to the wrong material.
    """
    header, items = _bom("TRAY", [("GPU", "4")], "1")
    orphan = {"bom_number": "999", "component_material_number": "PSU", "quantity_per": "2"}
    rolled = costing.roll_up([header], [*items, orphan], {"GPU": D("1000.00"), "PSU": D("99.00")})
    assert rolled == {"TRAY": D("4000.00")}


def test_applying_the_roll_up_replaces_every_drawn_window() -> None:
    """A manufactured material gets ONE window for the whole period.

    A standard is set at a period boundary and revised deliberately, not
    recalculated whenever a component's price moves -- which is also what
    leaves standard cost revaluation (ADR-023) something real to do.
    """
    start = dt.date(2026, 8, 1)
    standard_costs = [
        {
            "material_number": "RACK",
            "valid_from": start,
            "valid_to": dt.date(2026, 9, 30),
            "standard_cost_usd": D("8461.00"),
            "currency": "USD",
            "approved_by": "X",
            "approved_on": start,
        },
        {
            "material_number": "RACK",
            "valid_from": dt.date(2026, 10, 1),
            "valid_to": costing.OPEN_ENDED,
            "standard_cost_usd": D("8999.00"),
            "currency": "USD",
            "approved_by": "X",
            "approved_on": start,
        },
        {
            "material_number": "GPU",
            "valid_from": start,
            "valid_to": costing.OPEN_ENDED,
            "standard_cost_usd": D("1000.00"),
            "currency": "USD",
            "approved_by": "X",
            "approved_on": start,
        },
    ]
    lookup = {
        "RACK": [(start, dt.date(2026, 9, 30), D("8461.00"))],
        "GPU": [(start, costing.OPEN_ENDED, D("1000.00"))],
    }

    changed = costing.apply_to_standard_costs(
        standard_costs=standard_costs,
        rolled={"RACK": D("1163783.01")},
        cost_lookup=lookup,
        period_start=start,
        currency="USD",
        approved_by="COSTING",
    )

    assert changed == 1
    rack_rows = [r for r in standard_costs if r["material_number"] == "RACK"]
    assert len(rack_rows) == 1, "both drawn windows should be replaced by one"
    assert rack_rows[0]["standard_cost_usd"] == D("1163783.01")
    assert rack_rows[0]["valid_to"] == costing.OPEN_ENDED

    # The bought component must be untouched.
    gpu_rows = [r for r in standard_costs if r["material_number"] == "GPU"]
    assert len(gpu_rows) == 1
    assert gpu_rows[0]["standard_cost_usd"] == D("1000.00")

    # And the lookup every later valuation reads must agree with the rows, or
    # the ledger and the quantity-times-cost invariant diverge.
    assert lookup["RACK"] == [(start, costing.OPEN_ENDED, D("1163783.01"))]
    assert lookup["GPU"] == [(start, costing.OPEN_ENDED, D("1000.00"))]


def test_applying_nothing_changes_nothing() -> None:
    rows: list[dict] = []
    assert costing.apply_to_standard_costs(rows, {}, {}, dt.date(2026, 8, 1), "USD", "X") == 0
    assert rows == []
