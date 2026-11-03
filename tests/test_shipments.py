"""Inbound shipments and advance ship notices (BTB-34).

The property the extract exists to demonstrate is that an ASN **precedes** the
receipt it becomes. That is asserted per row, not per aggregate: comparing the
earliest ship date against the earliest receipt date would let one violation
hide inside a correct minimum, which is the same way a reconciled grand total
hides two orders that offset each other (ADR-001's amendment, CTRL-005).

Shipments are derived from the receipts that already exist (ADR-026), so
shipped quantity ties to received quantity exactly on a clean run. That
exactness is the point: a control whose baseline is noise cannot detect
anything. The tests below pin both the exactness and the one place it does not
apply -- an in-transit line, where nothing has arrived yet.

Deliberately absent: an on-time delivery figure asserted against a target.
Whether "on time" means exact or allows a tolerance window, and whether it is
measured by line or by shipment, are open questions recorded in
docs/metric_definitions.md for BTB-44. What is tested here is that the figure
is *computable* both ways, which is BTB-34's acceptance criterion -- and that
the share arriving late matches what config declares, so the generator cannot
quietly drift from its stated assumption.
"""

from __future__ import annotations

import collections
import datetime as dt
import itertools
from typing import Any

import pytest

from generator import config as genconfig
from generator import dataset, shipments

MVT_RECEIPT = "101"
MVT_ISSUE = "261"


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


@pytest.fixture(scope="module")
def built(cfg: genconfig.GeneratorConfig) -> dataset.Dataset:
    data, _ = dataset.build(cfg, "demo")
    return data


@pytest.fixture(scope="module")
def headers(built: dataset.Dataset) -> dict[str, dict[str, Any]]:
    return {h["inbound_delivery"]: h for h in built.as_mapping()["shipment_headers"]}


@pytest.fixture(scope="module")
def lines(built: dataset.Dataset) -> list[dict[str, Any]]:
    return built.as_mapping()["shipment_lines"]


@pytest.fixture(scope="module")
def receipts(built: dataset.Dataset) -> list[dict[str, Any]]:
    return [m for m in built.materialise()["goods_movements"] if m["movement_type"] == MVT_RECEIPT]


# ---------------------------------------------------------------------------
# The extract exists and carries what the story asked for
# ---------------------------------------------------------------------------
def test_shipments_are_generated(headers: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    assert headers, "no shipment headers were generated"
    assert lines, "no shipment lines were generated"


def test_shipments_carry_promised_and_actual_dates_carrier_and_quantity(
    headers: dict[str, dict[str, Any]],
) -> None:
    """BTB-34's first acceptance criterion, column by column."""
    for delivery, header in headers.items():
        assert header["carrier_code"], f"{delivery} has no carrier"
        assert header["carrier_name"], f"{delivery} has no carrier name"
        assert isinstance(header["shipped_date"], dt.date), f"{delivery} has no ship date"
        assert isinstance(header["promised_arrival_date"], dt.date), (
            f"{delivery} carries no promised date, so lateness cannot be computed"
        )
        assert header["total_quantity_shipped"] > 0, f"{delivery} ships nothing"


def test_the_promised_date_is_not_simply_the_purchase_order_date(
    built: dataset.Dataset, lines: list[dict[str, Any]], headers: dict[str, dict[str, Any]]
) -> None:
    """The ASN's promise and the order's agreed date must be distinguishable.

    If the generator set the promised arrival to the purchase order's delivery
    date, "on time against the promise" and "on time against the order" would
    be the same number, and carrying both would be pretence. A supplier that
    re-promises is exactly the case delivery performance has to express.
    """
    po_dates = {
        (line["po_number"], line["po_item"]): line["delivery_date"]
        for line in built.as_mapping()["purchase_order_lines"]
    }
    differing = 0
    compared = 0
    for line in lines:
        agreed = po_dates.get((line["po_number"], line["po_item"]))
        if agreed is None:
            continue
        compared += 1
        if headers[line["inbound_delivery"]]["promised_arrival_date"] != agreed:
            differing += 1
    assert compared > 0
    assert differing > 0, (
        "the ASN's promised date never differs from the purchase order's date, "
        "so the two measures of lateness are the same number"
    )


# ---------------------------------------------------------------------------
# ASNs precede their receipts -- per row
# ---------------------------------------------------------------------------
def test_every_asn_ships_before_its_receipt_posts(
    headers: dict[str, dict[str, Any]], receipts: list[dict[str, Any]]
) -> None:
    """BTB-34's second acceptance criterion, asserted per receipt.

    Strictly before, not on or before: a shipment arriving the day it ships has
    no transit time, and an ASN that coincides with its receipt is not advance
    notice of anything.
    """
    checked = 0
    for receipt in receipts:
        delivery = receipt["inbound_delivery"]
        if delivery is None:
            continue
        checked += 1
        shipped = headers[delivery]["shipped_date"]
        assert shipped < receipt["posting_date"], (
            f"delivery {delivery} shipped {shipped} but its receipt posted "
            f"{receipt['posting_date']}"
        )
    assert checked > 0, "no receipt carried a delivery, so nothing was checked"


def test_transit_time_is_always_at_least_one_day(
    headers: dict[str, dict[str, Any]],
) -> None:
    for delivery, header in headers.items():
        if header["arrived_date"] is None:
            continue
        assert header["arrived_date"] > header["shipped_date"], (
            f"{delivery} arrived the day it shipped"
        )


# ---------------------------------------------------------------------------
# Quantities tie
# ---------------------------------------------------------------------------
def test_shipped_quantity_ties_to_received_per_order_line(
    headers: dict[str, dict[str, Any]], lines: list[dict[str, Any]], receipts: list[dict[str, Any]]
) -> None:
    """Exact, per line -- not just in total.

    A grand total can reconcile while individual lines are wrong in opposite
    directions, so the comparison has to be at the grain a control would key
    on.
    """
    shipped = {
        (line["inbound_delivery"], line["po_number"], line["po_item"]): line["quantity_shipped"]
        for line in lines
    }
    received: collections.Counter[tuple[str, Any, Any]] = collections.Counter()
    for receipt in receipts:
        if receipt["inbound_delivery"]:
            key = (receipt["inbound_delivery"], receipt["po_number"], receipt["po_item"])
            received[key] += abs(int(receipt["quantity"]))

    compared = 0
    for key, quantity in shipped.items():
        if headers[key[0]]["status"] != shipments.STATUS_RECEIVED:
            continue
        compared += 1
        assert quantity == received[key], (
            f"delivery {key[0]} line {key[1]}/{key[2]}: shipped {quantity}, "
            f"received {received[key]}"
        )
    assert compared > 0


def test_a_delivery_line_is_unique_on_its_order_line(lines: list[dict[str, Any]]) -> None:
    """Merging receipt splits is what makes the reconciliation key usable.

    Two receipt splits of one order line can land on the same delivery: the
    splits are pushed apart by a day or more, but both are clamped into the
    reporting window, so splits of a line ordered near period end collide. One
    shipment line per receipt left duplicate order lines on two deliveries,
    found by the reconciliation above disagreeing.
    """
    keys = collections.Counter(
        (line["inbound_delivery"], line["po_number"], line["po_item"]) for line in lines
    )
    duplicated = [key for key, count in keys.items() if count > 1]
    assert not duplicated, f"deliveries with two lines for one order line: {duplicated[:3]}"


def test_the_header_total_equals_the_sum_of_its_lines(
    headers: dict[str, dict[str, Any]], lines: list[dict[str, Any]]
) -> None:
    totals: collections.Counter[str] = collections.Counter()
    for line in lines:
        totals[line["inbound_delivery"]] += line["quantity_shipped"]
    for delivery, header in headers.items():
        assert header["total_quantity_shipped"] == totals[delivery], (
            f"{delivery}: header says {header['total_quantity_shipped']}, "
            f"lines sum to {totals[delivery]}"
        )


def test_no_shipped_quantity_is_zero_or_negative(lines: list[dict[str, Any]]) -> None:
    """A shipped quantity counts goods on a truck.

    Unlike a movement quantity, which is signed by movement type, so a negative
    here would mean the sign was copied rather than the count.
    """
    for line in lines:
        assert line["quantity_shipped"] > 0, f"{line['inbound_delivery']} ships {line}"


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------
def test_supplier_foreign_keys_are_populated_when_defects_are_off(
    built: dataset.Dataset, headers: dict[str, dict[str, Any]]
) -> None:
    """BTB-34's fourth acceptance criterion.

    The column is nullable on purpose -- the `missing_supplier_on_shipment`
    defect nulls it so CTRL-009 can detect it -- which is exactly why a clean
    run has to be asserted non-null here. Without this, a generator that never
    populated the supplier would look the same as one that populated it
    correctly.
    """
    suppliers = {s["supplier_number"] for s in built.as_mapping()["suppliers"]}
    assert suppliers
    for delivery, header in headers.items():
        assert header["supplier_number"], f"{delivery} has no supplier on a clean run"
        assert header["supplier_number"] in suppliers, (
            f"{delivery} names supplier {header['supplier_number']}, which does not exist"
        )


def test_every_shipment_line_references_a_real_purchase_order_line(
    built: dataset.Dataset, lines: list[dict[str, Any]]
) -> None:
    po_lines = {
        (line["po_number"], line["po_item"]) for line in built.as_mapping()["purchase_order_lines"]
    }
    for line in lines:
        assert (line["po_number"], line["po_item"]) in po_lines, (
            f"{line['inbound_delivery']} item {line['shipment_item']} references "
            f"PO {line['po_number']}/{line['po_item']}, which does not exist"
        )


def test_every_shipment_line_belongs_to_a_header(
    headers: dict[str, dict[str, Any]], lines: list[dict[str, Any]]
) -> None:
    for line in lines:
        assert line["inbound_delivery"] in headers, (
            f"line references delivery {line['inbound_delivery']}, which has no header"
        )


def test_every_header_has_at_least_one_line(
    headers: dict[str, dict[str, Any]], lines: list[dict[str, Any]]
) -> None:
    """A delivery with no lines is a truck carrying nothing."""
    with_lines = {line["inbound_delivery"] for line in lines}
    empty = sorted(set(headers) - with_lines)
    assert not empty, f"deliveries with no lines: {empty[:3]}"


# ---------------------------------------------------------------------------
# The nullable column, from both sides
# ---------------------------------------------------------------------------
def test_every_receipt_against_a_known_order_names_a_delivery(
    built: dataset.Dataset, receipts: list[dict[str, Any]]
) -> None:
    po_lines = {
        (str(line["po_number"]), line["po_item"])
        for line in built.as_mapping()["purchase_order_lines"]
    }
    for receipt in receipts:
        if (str(receipt["po_number"]), receipt["po_item"]) not in po_lines:
            continue
        assert receipt["inbound_delivery"], (
            f"receipt {receipt['material_document']}/{receipt['material_document_item']} "
            "has no inbound delivery"
        )


def test_no_issue_carries_an_inbound_delivery(built: dataset.Dataset) -> None:
    """The other half of the nullability story.

    Asserting receipts are populated says nothing about issues being empty. A
    stamping bug that wrote the delivery onto every row would satisfy the test
    above while making every issue look like it arrived from a supplier -- and
    those issues would then be counted in delivery performance.
    """
    issues = [m for m in built.materialise()["goods_movements"] if m["movement_type"] == MVT_ISSUE]
    assert issues, "no issues were generated, so this proves nothing"
    for issue in issues:
        assert issue["inbound_delivery"] is None, (
            f"issue {issue['material_document']}/{issue['material_document_item']} "
            f"references delivery {issue['inbound_delivery']}"
        )


def test_an_unstamped_receipt_raises_rather_than_loading_quietly() -> None:
    """The guard that makes the nullable column safe.

    Because nulls are legitimate on issues, a population failure would load
    cleanly and be invisible. `apply_delivery_numbers` raises instead, so the
    failure is loud at generation time rather than a quiet null in the
    warehouse.
    """
    movements = [
        {
            "material_document": "4900000001",
            "fiscal_year": 2026,
            "material_document_item": 1,
            "movement_type": MVT_RECEIPT,
            "po_number": "4500000001",
            "po_item": 10,
        }
    ]
    outcome = shipments.ShipmentOutcome(
        headers=[],
        # A line for the same purchase order, so the receipt is one the
        # generator should have stamped.
        lines=[{"po_number": "4500000001", "po_item": 10}],
        delivery_by_movement={},
    )
    with pytest.raises(shipments.UnstampedReceiptError) as raised:
        shipments.apply_delivery_numbers(movements, outcome)
    assert "4900000001" in str(raised.value), "the error must name the offending receipt"


# ---------------------------------------------------------------------------
# In transit versus received
# ---------------------------------------------------------------------------
def test_status_agrees_with_arrival_and_with_the_receipts(
    headers: dict[str, dict[str, Any]], receipts: list[dict[str, Any]]
) -> None:
    stamped = {r["inbound_delivery"] for r in receipts if r["inbound_delivery"]}
    for delivery, header in headers.items():
        if header["status"] == shipments.STATUS_RECEIVED:
            assert header["arrived_date"] is not None, f"{delivery} is RECEIVED with no arrival"
            assert delivery in stamped, f"{delivery} is RECEIVED but no receipt names it"
        else:
            assert header["arrived_date"] is None, (
                f"{delivery} is IN_TRANSIT but carries an arrival date, so it would "
                "be counted in on-time delivery"
            )
            assert delivery not in stamped, f"{delivery} is IN_TRANSIT but a receipt names it"


def test_some_deliveries_are_in_transit_and_some_are_received(
    headers: dict[str, dict[str, Any]],
) -> None:
    """Both cases have to occur, or the distinction is untested.

    An in-transit ASN is the reason the delivery-performance join is an outer
    one. With none in the data, an inner join would pass every test and then
    silently drop open commitments in production.
    """
    assert any(h["status"] == shipments.STATUS_IN_TRANSIT for h in headers.values())
    assert any(h["status"] == shipments.STATUS_RECEIVED for h in headers.values())


def test_an_in_transit_asn_is_not_simply_late_paperwork(
    cfg: genconfig.GeneratorConfig, headers: dict[str, dict[str, Any]]
) -> None:
    """An in-transit shipment must be one that could not have arrived yet.

    If an in-transit ASN promised arrival in the middle of the window, the
    missing receipt would be a defect rather than an open commitment -- and
    CTRL-009-style detection would be right to flag it. Shipping late enough
    that the promise falls at or after period end is what makes it legitimate.
    """
    from generator import inventory

    generator = inventory.InventoryGenerator(cfg, "demo")
    in_transit = [h for h in headers.values() if h["status"] == shipments.STATUS_IN_TRANSIT]
    assert in_transit
    for header in in_transit:
        assert header["promised_arrival_date"] >= generator.end, (
            f"{header['inbound_delivery']} promised arrival "
            f"{header['promised_arrival_date']}, before period end {generator.end}, "
            "so a missing receipt is a finding rather than an open commitment"
        )


# ---------------------------------------------------------------------------
# On-time delivery is computable -- without defining the metric
# ---------------------------------------------------------------------------
def test_on_time_delivery_is_computable_by_shipment(
    headers: dict[str, dict[str, Any]],
) -> None:
    """BTB-34's third acceptance criterion, by shipment."""
    rate = shipments.on_time_rate(list(headers.values()))
    assert 0.0 < rate < 100.0, (
        f"on-time delivery came out at {rate}%, which means either every shipment "
        "or no shipment met its promise -- neither exercises the measure"
    )


def test_on_time_delivery_is_computable_by_line(
    headers: dict[str, dict[str, Any]], lines: list[dict[str, Any]]
) -> None:
    """And by line, because BTB-44 has not yet chosen the grain.

    Both groupings have to be available from what the extract carries, or that
    decision would already be made by the data.
    """
    arrived = [
        headers[line["inbound_delivery"]]
        for line in lines
        if headers[line["inbound_delivery"]]["arrived_date"] is not None
    ]
    assert arrived
    on_time = sum(1 for h in arrived if h["arrived_date"] <= h["promised_arrival_date"])
    rate = 100.0 * on_time / len(arrived)
    assert 0.0 < rate < 100.0

    # The two grains must not be identical, or "by line or by shipment" is not
    # a real question and carrying both grains is pretence.
    by_shipment = shipments.on_time_rate(list(headers.values()))
    assert rate != by_shipment, (
        "the by-line and by-shipment rates are identical, so the grain makes no "
        "difference and one of them is redundant"
    )


def test_the_late_share_matches_what_config_declares(
    cfg: genconfig.GeneratorConfig, headers: dict[str, dict[str, Any]]
) -> None:
    """The generator must not drift from its stated assumption.

    A first attempt drew transit slip from a uniform -1..+6 window, which put
    on-time delivery at 24% -- worse than a coin flip, and no manufacturer
    would recognise it. The share that runs late is configuration precisely so
    this test can hold the generator to it.
    """
    declared = float(cfg.domain["supplier_late_delivery_rate"])
    measured = (100.0 - shipments.on_time_rate(list(headers.values()))) / 100.0
    assert abs(measured - declared) < 0.03, (
        f"{measured:.1%} of deliveries arrived late against a declared {declared:.1%}"
    )


def test_lateness_is_not_symmetric_around_the_promise(
    headers: dict[str, dict[str, Any]],
) -> None:
    """Late arrivals should be the exception and the tail should be long.

    A symmetric distribution would put on-time delivery near 50%. This pins the
    shape rather than only the rate, so a change that kept the mean while
    flattening the distribution would still be caught.
    """
    deltas = [
        (h["arrived_date"] - h["promised_arrival_date"]).days
        for h in headers.values()
        if h["arrived_date"] is not None
    ]
    assert deltas
    late = [d for d in deltas if d > 0]
    early_or_on_time = [d for d in deltas if d <= 0]
    assert len(early_or_on_time) > len(late) * 3, (
        "late arrivals are not the exception, so the distribution is not one a "
        "supply chain would recognise"
    )
    assert max(late) >= 3, "no shipment is meaningfully late, so the tail is missing"


# ---------------------------------------------------------------------------
# Determinism and shape
# ---------------------------------------------------------------------------
def test_delivery_numbers_rise_with_arrival_date(
    headers: dict[str, dict[str, Any]],
) -> None:
    """As a real number range does, so a reviewer can read the order off the key."""
    arrived = sorted(
        (h for h in headers.values() if h["arrived_date"] is not None),
        key=lambda h: h["inbound_delivery"],
    )
    for earlier, later in itertools.pairwise(arrived):
        assert earlier["arrived_date"] <= later["arrived_date"], (
            f"{later['inbound_delivery']} arrived before {earlier['inbound_delivery']}"
        )


def test_a_delivery_can_span_more_than_one_purchase_order(
    lines: list[dict[str, Any]],
) -> None:
    """One truck brings several orders, which is what makes the grains differ.

    Grouping by purchase order instead of by supplier would make "one row per
    delivery" and "one row per delivery line plus one" the same shape, and the
    header/line grain story would prove nothing.
    """
    orders_per_delivery: dict[str, set[str]] = collections.defaultdict(set)
    for line in lines:
        orders_per_delivery[line["inbound_delivery"]].add(line["po_number"])
    spanning = [d for d, orders in orders_per_delivery.items() if len(orders) > 1]
    assert spanning, "no delivery carries more than one purchase order"


def test_generation_is_reproducible(cfg: genconfig.GeneratorConfig) -> None:
    """Same seed, same shipments.

    The shipment generator takes its own seed (ADR-001's amendment), so this
    also pins that the numbers do not shift when an earlier generator's draw
    count changes.
    """
    first, _ = dataset.build(cfg, "demo")
    second, _ = dataset.build(cfg, "demo")
    assert first.as_mapping()["shipment_headers"] == second.as_mapping()["shipment_headers"]
    assert first.as_mapping()["shipment_lines"] == second.as_mapping()["shipment_lines"]


def test_carriers_are_used_and_ocean_freight_is_the_minority(
    headers: dict[str, dict[str, Any]],
) -> None:
    """Every carrier should appear, with ocean freight a small share.

    Transit time drives the on-time distribution, so a uniform draw across four
    carriers would make a quarter of all inbound freight a three-week crossing
    and that would dominate every delivery-performance figure Phase 3 computes.
    """
    counts = collections.Counter(h["carrier_code"] for h in headers.values())
    assert len(counts) == 4, f"not every carrier was used: {dict(counts)}"
    total = sum(counts.values())
    assert counts["OCEN"] / total < 0.2, f"ocean freight is {counts['OCEN'] / total:.0%} of freight"


def test_shipments_load_before_the_movements_that_reference_them() -> None:
    """A receipt references a delivery, so the deliveries must land first.

    Generation order is the reverse -- deliveries are derived from receipts --
    which is why load order and generation order are separate concerns
    (ADR-026). Reversing this would make the referencing column point at rows
    that are not there yet.
    """
    order = list(dataset.LOAD_ORDER)
    assert order.index("shipment_headers") < order.index("goods_movements")
    assert order.index("shipment_lines") < order.index("goods_movements")
    assert order.index("purchase_order_lines") < order.index("shipment_lines")
    assert order.index("suppliers") < order.index("shipment_headers")
