"""Inbound shipments and advance ship notices (BTB-34).

Shaped on SAP's inbound delivery, LIKP and LIPS, per ADR-002: a header
describes one supplier's shipment, and its items list what is on the truck,
each referencing the purchase order line it fulfils.

An ASN is a promise that arrives before the goods do. That ordering is the
whole point of the extract, so it is keyed rather than inferred: every goods
receipt carries the `inbound_delivery` it was received against (ADR-026), and
a test asserts per row that the delivery shipped before the receipt posted.
Comparing aggregates instead would let one violation hide inside a correct
minimum -- the same failure mode as reconciling a grand total while two orders
offset each other (ADR-001's amendment).

Deliveries are **derived from the receipts that already exist**, not generated
independently and matched up afterwards. Two reasons:

* Shipped quantity then ties to received quantity exactly, so a future
  shipment-discrepancy control has a clean baseline instead of one made of
  noise (the lesson CTRL-005 is built on).
* No second row count to disagree with the first. `config.yaml` declares no
  shipment count on purpose: declaring both `mes_events` and an order quantity
  put the two 32x apart with nothing to notice (see `mes.py`).

A delivery groups receipts by **supplier and arrival date**, not by purchase
order. One truck brings several purchase orders, which is why the header and
line grains differ by more than a constant -- and a grain story where "one row
per delivery" and "one row per delivery line" are the same number proves
nothing.

In-transit ASNs come from purchase order lines that were never received inside
the window. About 12% of ordered lines are not delivered in the period, and a
share of those are legitimately on the water: shipped, promised, not yet
arrived. Those deliveries have no receipt and a null arrival date, which makes
them a real open commitment rather than a defect to be detected.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

import numpy as np

from generator import ids
from generator.writers import ExtractSpec

#: Movement type for a goods receipt against a purchase order. Issues (261)
#: have no inbound delivery, because nothing arrived from a supplier.
MVT_GOODS_RECEIPT = "101"

#: Carriers, with the transit time each one plans for.
#:
#: Planned transit is what makes an ASN's promised arrival computable: the
#: supplier ships, adds the carrier's planned transit, and commits to that
#: date. Lateness is then actual transit minus planned transit, which is a
#: property of the shipment rather than a number drawn from nowhere.
_CARRIERS: tuple[tuple[str, str, int], ...] = (
    ("EXPR", "Expedited Air", 3),
    ("STDA", "Standard Air", 7),
    ("GRND", "Ground Freight", 5),
    ("OCEN", "Ocean Freight", 21),
)

#: Share of carrier choices that go to ocean freight.
#:
#: Weighted rather than uniform because transit time drives the on-time
#: distribution, and a uniform draw would make a quarter of all inbound
#: freight a three-week ocean crossing -- which would then dominate every
#: delivery-performance figure Phase 3 computes.
_OCEAN_SHARE = 0.12

#: Transit variance, in days, split by whether the shipment runs late.
#:
#: Two distributions rather than one window, because lateness is the exception
#: and a single window cannot express that. A uniform -1..+6 draw was the first
#: attempt: it put on-time delivery at 24%, worse than a coin flip. The share
#: that runs late is configuration (`supplier_late_delivery_rate`), since
#: supplier on-time delivery is a Phase 3 KPI and the assumption behind it
#: should be visible without reading this file.
#:
#: An on-time shipment may still arrive early, which is why the on-time window
#: is not simply zero -- a supply chain where nothing ever arrives ahead of
#: schedule is as unconvincing as one where nothing arrives on time.
_ON_TIME_SLIP = (-2, 0)
_LATE_SLIP = (1, 9)

#: Share of never-received purchase order lines that are on the water at
#: period end rather than simply never shipped.
_IN_TRANSIT_SHARE = 0.45

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "shipment_headers": ExtractSpec(
        name="shipment_headers",
        columns=[
            "inbound_delivery",
            "supplier_number",
            "carrier_code",
            "carrier_name",
            "shipped_date",
            "promised_arrival_date",
            "arrived_date",
            "planned_transit_days",
            "status",
            "total_quantity_shipped",
        ],
        source_system="SUPPLY_CHAIN",
        # Shipments carry no money -- an ASN is a promise about goods, and the
        # price was agreed on the purchase order. Completeness rests on the
        # row count, which is what the sidecar's primary check becomes.
        value_column=None,
        natural_key=["inbound_delivery"],
    ),
    "shipment_lines": ExtractSpec(
        name="shipment_lines",
        columns=[
            "inbound_delivery",
            "shipment_item",
            "po_number",
            "po_item",
            "material_number",
            "quantity_shipped",
            "uom",
        ],
        source_system="SUPPLY_CHAIN",
        value_column=None,
        natural_key=["inbound_delivery", "shipment_item"],
    ),
}

STATUS_RECEIVED = "RECEIVED"
STATUS_IN_TRANSIT = "IN_TRANSIT"

#: The natural key of a goods movement line: document numbers repeat across
#: fiscal years (ADR-020), so the year is part of the key.
MovementKey = tuple[str, int, int]


@dataclasses.dataclass
class ShipmentOutcome:
    """Shipment headers and lines, plus which delivery each receipt belongs to.

    `delivery_by_movement` is returned rather than written straight onto the
    movement rows so that applying it is a separate, checkable step:
    `apply_delivery_numbers` raises if a receipt would be left without a
    delivery, which a silent in-place stamp could not do.
    """

    headers: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    delivery_by_movement: dict[MovementKey, str]

    def received_count(self) -> int:
        return sum(1 for header in self.headers if header["status"] == STATUS_RECEIVED)

    def in_transit_count(self) -> int:
        return sum(1 for header in self.headers if header["status"] == STATUS_IN_TRANSIT)


def movement_key(movement: dict[str, Any]) -> MovementKey:
    """The natural key of a movement line."""
    return (
        str(movement["material_document"]),
        int(movement["fiscal_year"]),
        int(movement["material_document_item"]),
    )


class ShipmentGenerator:
    """Builds inbound deliveries from the receipts that already exist."""

    def __init__(
        self,
        rng: Any,
        period_start: dt.date,
        period_end: dt.date,
        late_rate: float,
    ) -> None:
        self.rng = rng
        self.start = period_start
        self.end = period_end
        #: Share of deliveries that arrive after the promised date. From
        #: config, not hardcoded: it is the assumption behind a Phase 3 KPI.
        self.late_rate = late_rate
        self._delivery_numbers = ids.inbound_delivery_numbers()

    # -- helpers ----------------------------------------------------------
    def _carrier(self) -> tuple[str, str, int]:
        """Pick a carrier, weighting ocean freight down to a realistic share."""
        if self.rng.random() < _OCEAN_SHARE:
            return _CARRIERS[3]
        return _CARRIERS[int(self.rng.integers(0, 3))]

    def _actual_transit(self, planned: int) -> int:
        """Days actually taken. At least one: nothing arrives the day it ships.

        A zero-day transit would make `shipped_date` equal the receipt's
        posting date, and the invariant this extract exists to demonstrate is
        that an ASN *precedes* its receipt -- not that it coincides with it.
        """
        window = _LATE_SLIP if self.rng.random() < self.late_rate else _ON_TIME_SLIP
        slip = int(self.rng.integers(window[0], window[1] + 1))
        return max(1, planned + slip)

    # -- generation -------------------------------------------------------
    def generate(
        self,
        movements: Sequence[dict[str, Any]],
        po_headers: Sequence[dict[str, Any]],
        po_lines: Sequence[dict[str, Any]],
    ) -> ShipmentOutcome:
        """Build deliveries for every receipt, plus in-transit ASNs.

        Args:
            movements: Goods movement lines. Receipts carry `po_number`.
            po_headers: Purchase order headers, which hold the supplier.
            po_lines: Purchase order lines, for the ones never received.
        """
        supplier_of_po = {
            str(header["po_number"]): str(header["supplier_number"]) for header in po_headers
        }

        headers: list[dict[str, Any]] = []
        lines: list[dict[str, Any]] = []
        delivery_by_movement: dict[MovementKey, str] = {}

        self._deliveries_for_receipts(
            movements, supplier_of_po, headers, lines, delivery_by_movement
        )
        self._deliveries_in_transit(movements, po_lines, supplier_of_po, headers, lines)

        return ShipmentOutcome(
            headers=headers, lines=lines, delivery_by_movement=delivery_by_movement
        )

    def _deliveries_for_receipts(
        self,
        movements: Sequence[dict[str, Any]],
        supplier_of_po: dict[str, str],
        headers: list[dict[str, Any]],
        lines: list[dict[str, Any]],
        delivery_by_movement: dict[MovementKey, str],
    ) -> None:
        """One delivery per supplier per arrival date."""
        groups: dict[tuple[str, dt.date], list[dict[str, Any]]] = {}
        for movement in movements:
            if movement["movement_type"] != MVT_GOODS_RECEIPT:
                continue
            po_number = str(movement["po_number"])
            supplier = supplier_of_po.get(po_number)
            if supplier is None:
                # A receipt whose purchase order does not exist. The
                # `receipt_without_po` defect plants exactly this, so it must
                # not crash the generator -- it has to reach the warehouse for
                # CTRL-004 to find it. Such a receipt gets no delivery.
                continue
            groups.setdefault((supplier, movement["posting_date"]), []).append(movement)

        # Sorted so delivery numbers rise with arrival date, as a real number
        # range does, and so the output does not depend on dict iteration order.
        for supplier, arrived_on in sorted(groups, key=lambda key: (key[1], key[0])):
            receipts = groups[(supplier, arrived_on)]
            code, name, planned_transit = self._carrier()
            transit = self._actual_transit(planned_transit)
            shipped_on = arrived_on - dt.timedelta(days=transit)
            delivery = self._delivery_numbers.next()

            headers.append(
                {
                    "inbound_delivery": delivery,
                    "supplier_number": supplier,
                    "carrier_code": code,
                    "carrier_name": name,
                    "shipped_date": shipped_on,
                    # The supplier's own commitment: ship date plus the
                    # carrier's planned transit. Distinct from the purchase
                    # order's delivery date, which is what was agreed when the
                    # order was placed -- so on-time delivery can be measured
                    # against either (docs/metric_definitions.md, BTB-44).
                    "promised_arrival_date": shipped_on + dt.timedelta(days=planned_transit),
                    "arrived_date": arrived_on,
                    "planned_transit_days": planned_transit,
                    "status": STATUS_RECEIVED,
                    "total_quantity_shipped": sum(abs(int(r["quantity"])) for r in receipts),
                }
            )

            # One line per purchase order line, not one per receipt row.
            #
            # A delivery can carry two receipt rows for the same order line:
            # receipt splits are pushed apart by a day or more, but both are
            # clamped into the reporting window, so two splits of a line
            # ordered near period end can land on the same date. SAP's LIPS
            # would carry one item with the combined quantity, and merging
            # makes (delivery, po_number, po_item) a unique reconciliation key
            # -- which is what a shipped-versus-received control has to join
            # on. Emitting one line per receipt left two deliveries with
            # duplicate order lines, found by the reconciliation disagreeing.
            merged: dict[tuple[str, int], dict[str, Any]] = {}
            for receipt in receipts:
                key = (str(receipt["po_number"]), int(receipt["po_item"]))
                if key in merged:
                    # Receipt quantities are signed by movement type; a
                    # shipped quantity is a count of things on a truck and is
                    # never negative.
                    merged[key]["quantity_shipped"] += abs(int(receipt["quantity"]))
                else:
                    merged[key] = {
                        "po_number": receipt["po_number"],
                        "po_item": receipt["po_item"],
                        "material_number": receipt["material_number"],
                        "quantity_shipped": abs(int(receipt["quantity"])),
                        "uom": receipt["uom"],
                    }
                delivery_by_movement[movement_key(receipt)] = delivery

            for item, row in zip(ids.line_items(len(merged)), merged.values(), strict=True):
                lines.append({"inbound_delivery": delivery, "shipment_item": item, **row})

    def _deliveries_in_transit(
        self,
        movements: Sequence[dict[str, Any]],
        po_lines: Sequence[dict[str, Any]],
        supplier_of_po: dict[str, str],
        headers: list[dict[str, Any]],
        lines: list[dict[str, Any]],
    ) -> None:
        """ASNs for order lines still on the water at period end."""
        received_lines = {
            (str(m["po_number"]), m["po_item"])
            for m in movements
            if m["movement_type"] == MVT_GOODS_RECEIPT
        }
        undelivered = [
            line
            for line in po_lines
            if (str(line["po_number"]), line["po_item"]) not in received_lines
        ]

        for line in undelivered:
            if self.rng.random() >= _IN_TRANSIT_SHARE:
                continue
            supplier = supplier_of_po.get(str(line["po_number"]))
            if supplier is None:
                continue

            code, name, planned_transit = self._carrier()
            # Shipped late enough that the planned arrival falls at or after
            # period end -- which is *why* it has not been received. A ship
            # date that implies arrival mid-window would make the missing
            # receipt a defect rather than an open commitment.
            latest_ship = self.end
            earliest_ship = max(
                line["delivery_date"],
                self.end - dt.timedelta(days=max(1, planned_transit - 1)),
            )
            if earliest_ship > latest_ship:
                earliest_ship = latest_ship
            span = (latest_ship - earliest_ship).days
            shipped_on = earliest_ship + dt.timedelta(
                days=int(self.rng.integers(0, span + 1)) if span > 0 else 0
            )
            delivery = self._delivery_numbers.next()

            headers.append(
                {
                    "inbound_delivery": delivery,
                    "supplier_number": supplier,
                    "carrier_code": code,
                    "carrier_name": name,
                    "shipped_date": shipped_on,
                    "promised_arrival_date": shipped_on + dt.timedelta(days=planned_transit),
                    # Null, not the period end: nothing has arrived. A date
                    # here would make an in-transit shipment look received and
                    # would be counted in on-time delivery.
                    "arrived_date": None,
                    "planned_transit_days": planned_transit,
                    "status": STATUS_IN_TRANSIT,
                    "total_quantity_shipped": int(line["quantity_ordered"]),
                }
            )
            lines.append(
                {
                    "inbound_delivery": delivery,
                    "shipment_item": ids.line_items(1)[0],
                    "po_number": line["po_number"],
                    "po_item": line["po_item"],
                    "material_number": line["material_number"],
                    "quantity_shipped": int(line["quantity_ordered"]),
                    "uom": line["uom"],
                }
            )


class UnstampedReceiptError(Exception):
    """A goods receipt was left without an inbound delivery."""


def apply_delivery_numbers(
    movements: Sequence[dict[str, Any]],
    outcome: ShipmentOutcome,
) -> None:
    """Write `inbound_delivery` onto every movement row, in place.

    Receipts get the delivery they arrived on; issues get None, because an
    issue to a production order has nothing to do with a supplier shipment
    (ADR-026).

    Raises:
        UnstampedReceiptError: if a receipt whose purchase order exists was
            left without a delivery. The column is nullable, so a population
            failure would otherwise be indistinguishable from the legitimate
            nulls on issues -- and the extract would load cleanly while the
            link the story rests on was simply missing.
    """
    known_pos = {str(line["po_number"]) for line in outcome.lines}
    unstamped: list[MovementKey] = []

    for movement in movements:
        if movement["movement_type"] != MVT_GOODS_RECEIPT:
            movement["inbound_delivery"] = None
            continue
        key = movement_key(movement)
        delivery = outcome.delivery_by_movement.get(key)
        movement["inbound_delivery"] = delivery
        if delivery is None and str(movement["po_number"]) in known_pos:
            unstamped.append(key)

    if unstamped:
        raise UnstampedReceiptError(
            f"{len(unstamped)} goods receipt(s) have no inbound delivery, e.g. {unstamped[:3]}"
        )


def on_time_rate(headers: Sequence[dict[str, Any]]) -> float:
    """Share of arrived deliveries that met the promised date, as a percentage.

    Here to show the figure is computable from what the extract carries, which
    is BTB-34's acceptance criterion. It is **not** the metric definition:
    whether on time means exact or within a tolerance window, and whether it is
    measured by line or by shipment, are open questions recorded in
    docs/metric_definitions.md for BTB-44. This takes the strictest reading --
    arrived on or before promised -- and counts by shipment.
    """
    arrived = [h for h in headers if h["arrived_date"] is not None]
    if not arrived:
        return 0.0
    on_time = sum(1 for h in arrived if h["arrived_date"] <= h["promised_arrival_date"])
    return 100.0 * on_time / len(arrived)


def build(
    config: Any,
    movements: Sequence[dict[str, Any]],
    po_headers: Sequence[dict[str, Any]],
    po_lines: Sequence[dict[str, Any]],
    period_start: dt.date,
    period_end: dt.date,
) -> ShipmentOutcome:
    """Generate the shipment extracts and stamp the movements that fed them."""
    generator = ShipmentGenerator(
        # A seed offset from the run's, so shipment numbers do not shift when an
        # earlier generator's draw count changes. ADR-001's amendment explains
        # why that independence is worth having.
        rng=np.random.default_rng(config.seed + 2),
        period_start=period_start,
        period_end=period_end,
        late_rate=float(config.domain["supplier_late_delivery_rate"]),
    )
    outcome = generator.generate(movements, po_headers, po_lines)
    apply_delivery_numbers(movements, outcome)
    return outcome
