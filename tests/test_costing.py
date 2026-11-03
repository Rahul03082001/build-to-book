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


# ---------------------------------------------------------------------------
# The standard-costing chain, end to end (ADR-028)
#
# Before this, production was invisible to finance: the factory completed 1,199
# units in the demo window and not one of them touched the books. A balance
# sheet built from it showed components bought and expensed, and no product.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def chain() -> dict[str, object]:
    """A full build with the MES stream consumed, so the deferred chain exists.

    The movements and the postings are deferred extracts now: finished goods
    receipts come from the confirmations, and a confirmation is only known once
    MES has recorded the outcome. Consuming the stream is what the writer does,
    and nothing downstream is available until it has.
    """
    from generator import config as genconfig
    from generator import dataset

    data, _ = dataset.build(genconfig.load(), "demo")
    events = sum(len(chunk) for chunk in data.streams["mes_unit_events"]())
    return {
        "config": genconfig.load(),
        "events": events,
        "confirmations": data.deferred["production_confirmations"](),
        "movements": data.deferred["goods_movements"](),
        "postings": data.deferred["gl_postings"](),
        "extracts": data.as_mapping(),
        "order_plan": data.order_plan,
    }


def _by_account(postings: list[dict]) -> dict[str, D]:
    import collections

    totals: dict[str, D] = collections.defaultdict(lambda: D(0))
    for row in postings:
        totals[row["gl_account"]] += row["signed_amount_usd"]
    return totals


def test_the_trial_balance_nets_to_zero(chain: dict) -> None:
    """Double entry, asserted on the whole ledger rather than per document.

    CTRL-002 already checks each document balances. This is the weaker-looking
    but independent statement: the sum of every signed amount is zero. A
    posting written to the wrong SIDE of a balanced pair would satisfy the
    per-document check and fail here.
    """
    totals = _by_account(chain["postings"])
    assert totals
    assert sum(totals.values()) == 0, f"trial balance is {sum(totals.values()):,}"


def test_work_in_process_empties_as_orders_close(chain: dict) -> None:
    """Every order absorbs components and relieves the lot.

    A non-zero balance means an order took in cost that was never relieved, so
    the value sits in an asset account with nothing to show for it. Every order
    finishes inside the window by construction, so there is no legitimate
    closing balance.
    """
    from generator import accounts

    totals = _by_account(chain["postings"])
    assert totals[accounts.WORK_IN_PROCESS.number] == 0, (
        f"work in process closes at {totals[accounts.WORK_IN_PROCESS.number]:,}"
    )


def test_a_component_issue_capitalises_rather_than_expensing(chain: dict) -> None:
    """ADR-028's central change, and the one most easily reverted by accident.

    Issuing a component to a production order moves it along the factory floor;
    it does not leave the business. Posting it to an expense account -- which is
    what this did before -- states that the money is gone, and makes cost of
    goods sold a function of purchasing rather than of what was sold.
    """
    from generator import accounts, inventory

    issue_documents = {
        row["material_document"]
        for row in chain["movements"]
        if row["movement_type"] == inventory.MVT_GOODS_ISSUE
    }
    assert issue_documents

    accounts_hit = {
        row["gl_account"]
        for row in chain["postings"]
        if row["reference_document"] in issue_documents
    }
    assert accounts_hit == {
        accounts.INVENTORY_RAW.number,
        accounts.WORK_IN_PROCESS.number,
    }, f"a component issue posted to {sorted(accounts_hit)}"
    assert accounts.COST_OF_GOODS_SOLD.number not in accounts_hit


def test_cost_of_goods_sold_is_posted_only_at_dispatch(chain: dict) -> None:
    """The whole point of recognising it at dispatch (ADR-028).

    If anything else posted to it, cost of sale would include goods that were
    built and never shipped -- which is the fault the old model had, where it
    included components bought and never used.
    """
    from generator import accounts
    from generator import costing as costing_module

    dispatch_documents = {
        row["material_document"]
        for row in chain["movements"]
        if row["movement_type"] == costing_module.MVT_DISPATCH
    }
    assert dispatch_documents

    cogs_references = {
        row["reference_document"]
        for row in chain["postings"]
        if row["gl_account"] == accounts.COST_OF_GOODS_SOLD.number
    }
    assert cogs_references
    assert cogs_references <= dispatch_documents, (
        f"cost of sale posted against {sorted(cogs_references - dispatch_documents)[:3]}, "
        "which are not dispatches"
    )


def test_every_confirmed_order_produces_finished_goods(chain: dict) -> None:
    """A confirmation with no receipt would be output that never reached stock.

    It would then be dispatched out of a balance nothing put there, which is
    the negative finished goods balance CTRL-010 exists to catch -- caught here
    at generation instead.
    """
    from generator import costing as costing_module

    confirmed_orders = {
        c["order_number"] for c in chain["confirmations"] if int(c["confirmed_quantity"]) > 0
    }
    assert confirmed_orders

    receipt_orders = {
        row["order_number"]
        for row in chain["movements"]
        if row["movement_type"] == costing_module.MVT_PRODUCTION_RECEIPT
        and row["order_number"] is not None
    }
    missing = sorted(confirmed_orders - receipt_orders)
    assert not missing, f"confirmed order(s) with no finished goods receipt: {missing[:3]}"


def test_no_order_dispatches_more_than_it_confirmed(chain: dict) -> None:
    """CTRL-010's premise for finished goods, asserted per order.

    An aggregate would let one order over-dispatch while another under-ships,
    and the total would look fine -- the same reason CTRL-005 and CTRL-018
    compare per order rather than in total.
    """
    import collections

    from generator import costing as costing_module

    confirmed = {c["order_number"]: int(c["confirmed_quantity"]) for c in chain["confirmations"]}
    dispatched: dict[str, int] = collections.Counter()
    for row in chain["movements"]:
        if row["movement_type"] == costing_module.MVT_DISPATCH:
            dispatched[row["order_number"]] += -int(row["quantity"])

    assert dispatched
    for order_number, quantity in dispatched.items():
        assert quantity <= confirmed.get(order_number, 0), (
            f"order {order_number} dispatched {quantity} against {confirmed.get(order_number, 0)} "
            "confirmed"
        )


def test_a_dispatch_never_precedes_the_output_it_ships(chain: dict) -> None:
    from generator import costing as costing_module

    confirmed_on = {c["order_number"]: c["posting_date"] for c in chain["confirmations"]}
    checked = 0
    for row in chain["movements"]:
        if row["movement_type"] != costing_module.MVT_DISPATCH:
            continue
        checked += 1
        assert row["posting_date"] > confirmed_on[row["order_number"]], (
            f"order {row['order_number']} dispatched {row['posting_date']} but confirmed "
            f"{confirmed_on[row['order_number']]}"
        )
    assert checked > 0


def test_the_dispatch_share_matches_what_config_declares(chain: dict) -> None:
    """The aggregate, because that is what a configured rate is about.

    Truncating per order gave 67% against a declared 85%: orders are about
    three units, and int(3 * 0.85) is 2. Rounding is no better -- round(3*0.85)
    is 3, which dispatches everything. The fraction is carried stochastically
    so the aggregate honours the rate.
    """
    from generator import costing as costing_module

    declared = float(chain["config"].domain["finished_goods_dispatch_rate"])
    confirmed = sum(int(c["confirmed_quantity"]) for c in chain["confirmations"])
    dispatched = sum(
        -int(row["quantity"])
        for row in chain["movements"]
        if row["movement_type"] == costing_module.MVT_DISPATCH
    )
    assert confirmed > 0
    measured = dispatched / confirmed
    assert abs(measured - declared) < 0.05, (
        f"{measured:.1%} of confirmed output was dispatched against a declared {declared:.0%}"
    )


def test_finished_goods_retains_what_was_not_dispatched(chain: dict) -> None:
    """A manufacturer ships from stock rather than emptying it.

    Dispatching everything would leave finished goods at zero every period,
    which is the fault `inventory_retention` exists to prevent for raw
    materials.
    """
    from generator import accounts

    totals = _by_account(chain["postings"])
    assert totals[accounts.FINISHED_GOODS.number] > 0, (
        "finished goods closes at zero, so nothing is held in stock"
    )
