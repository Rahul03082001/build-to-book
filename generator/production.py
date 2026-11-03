"""Production orders, component consumption and confirmations (BTB-32).

A production order builds a quantity of one rack configuration. Its component
consumption follows that configuration's exploded BOM, and its confirmations
record how many units were actually completed.

Three things are kept honest here because downstream controls depend on them:

* **Consumption follows the BOM.** Quantity consumed is the exploded
  requirement times the order quantity. If it did not, the Phase 3 variance
  metrics would be measuring the generator rather than the business.
* **Confirmations tie to MES.** With defects off, confirmed quantity equals
  the units that pass final test in MES (BTB-33). CTRL-005 compares those two
  figures, so a gap must mean a real finding rather than a generator
  coincidence. Confirmations are therefore generated *from* the MES outcome,
  not independently.
* **Scrap is explicit.** A unit that fails final test beyond its rework limit
  is scrapped, and scrap plus confirmed must equal the units started --
  otherwise units vanish and nobody can say where.

The BOM explosion is duplicated here in Python, deliberately and narrowly: the
generator needs component quantities to emit consumption rows, and it cannot
query a dbt model that does not exist yet. ADR-025's point stands -- the
*warehouse's* explosion is the reviewable one, and a dbt test checks the
warehouse against an independent calculation rather than against this code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from generator import ids
from generator.writers import ExtractSpec

ORDER_STATUS_RELEASED = "REL"
ORDER_STATUS_CONFIRMED = "CNF"
ORDER_STATUS_TECHNICALLY_COMPLETE = "TECO"

#: Movement type for a goods issue to a production order (ADR-002).
MVT_ISSUE_TO_ORDER = "261"

#: Days into the reporting window before the first production order starts.
#:
#: Components must be received before they can be issued to an order, and the
#: shortest purchase lead time is seven days (see `purchase_orders`). Three
#: weeks gives early orders stock to draw on without idling a month of a
#: three-month window.
PRODUCTION_RAMP_DAYS = 21

#: Days from order start to finish. A rack passes through assembly, firmware,
#: a twelve-hour burn-in and final test, so a few days is realistic.
ORDER_DURATION_DAYS = (3, 12)

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "production_orders": ExtractSpec(
        name="production_orders",
        columns=[
            "order_number",
            "material_number",
            "plant",
            "target_quantity",
            "uom",
            "order_date",
            "start_date",
            "finish_date",
            "status",
            "created_by",
        ],
        source_system="ERP",
        value_column=None,
        natural_key=["order_number"],
    ),
    "component_consumption": ExtractSpec(
        name="component_consumption",
        columns=[
            "order_number",
            "consumption_item",
            "component_material_number",
            "quantity_consumed",
            "uom",
            "movement_type",
            "posting_date",
            "entered_by",
        ],
        source_system="ERP",
        value_column=None,
        natural_key=["order_number", "consumption_item"],
    ),
    "production_confirmations": ExtractSpec(
        name="production_confirmations",
        columns=[
            "order_number",
            "confirmation_number",
            "confirmed_quantity",
            "scrap_quantity",
            "uom",
            "confirmation_date",
            "posting_date",
            "entered_by",
        ],
        source_system="ERP",
        value_column=None,
        natural_key=["order_number", "confirmation_number"],
    ),
}


@dataclasses.dataclass
class ProductionData:
    orders: list[dict[str, Any]]
    consumption: list[dict[str, Any]]
    confirmations: list[dict[str, Any]]

    #: order number -> (rack material, target quantity, start, finish). MES
    #: generates serials per order, so it needs this.
    order_plan: dict[str, dict[str, Any]]

    def as_mapping(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "production_orders": self.orders,
            "component_consumption": self.consumption,
            "production_confirmations": self.confirmations,
        }


def explode_bom(
    bom_headers: Sequence[dict[str, Any]],
    bom_items: Sequence[dict[str, Any]],
    top_material: str,
    max_depth: int = 10,
) -> dict[str, int]:
    """Total leaf-component quantities needed for one of `top_material`.

    A narrow Python mirror of `int_bom_exploded`, used only so the generator
    can emit consumption rows. The warehouse's explosion remains the
    reviewable one (ADR-025), and the dbt test verifies the warehouse against
    an independent SQL calculation rather than against this function -- so the
    two cannot pass by agreeing with each other.
    """
    header_by_parent = {h["material_number"]: h["bom_number"] for h in bom_headers}
    items_by_bom: dict[str, list[dict[str, Any]]] = {}
    for item in bom_items:
        items_by_bom.setdefault(item["bom_number"], []).append(item)

    requirements: dict[str, int] = {}

    def walk(material: str, multiplier: int, depth: int, seen: tuple[str, ...]) -> None:
        if depth > max_depth or material in seen:
            return
        bom_number = header_by_parent.get(material)
        if bom_number is None:
            # A leaf: a bought part.
            requirements[material] = requirements.get(material, 0) + multiplier
            return
        for item in items_by_bom.get(bom_number, []):
            walk(
                item["component_material_number"],
                multiplier * item["quantity_per"],
                depth + 1,
                (*seen, material),
            )

    bom_number = header_by_parent.get(top_material)
    if bom_number is None:
        return {}
    for item in items_by_bom.get(bom_number, []):
        walk(item["component_material_number"], item["quantity_per"], 1, (top_material,))
    return requirements


class ProductionGenerator:
    """Production orders over the rack configurations that have BOMs."""

    def __init__(
        self,
        bom_headers: Sequence[dict[str, Any]],
        bom_items: Sequence[dict[str, Any]],
        rack_configurations: dict[str, list[str]],
        rng: Any,
        plant: str,
        uom: str,
        period_start: dt.date,
        period_end: dt.date,
        order_count: int,
    ) -> None:
        self.rng = rng
        self.plant = plant
        self.uom = uom
        self.period_start = period_start
        self.period_end = period_end
        self.order_count = order_count
        self._numbers = ids.production_order_numbers()

        self.racks = sorted(rack_configurations)
        self._requirements = {
            rack: explode_bom(bom_headers, bom_items, rack) for rack in self.racks
        }

    def generate(self) -> ProductionData:
        orders: list[dict[str, Any]] = []
        consumption: list[dict[str, Any]] = []
        order_plan: dict[str, dict[str, Any]] = {}

        if not self.racks:
            return ProductionData(
                orders=orders, consumption=consumption, confirmations=[], order_plan={}
            )

        # Orders finish inside the window, so every one has its consumption and
        # its MES events in the reporting period.
        last_start = self.period_end - dt.timedelta(days=max(ORDER_DURATION_DAYS))

        # Production starts after a ramp, because components have to arrive
        # before they can be issued (ADR-027). Orders used to start on day one
        # of the window and consume immediately, while the earliest purchase
        # receipt could not land for a week -- an order consumed 444 units of a
        # component on 2026-08-01 against an on-hand balance of zero.
        #
        # That was invisible until the issues became real movements: with
        # consumption outside the material ledger, nothing compared the two.
        first_start = self.period_start + dt.timedelta(days=PRODUCTION_RAMP_DAYS)
        if first_start >= last_start:
            # A window too short to ramp. Keep the original behaviour rather
            # than producing no orders at all, and let the on-hand guard in
            # dataset.build decide whether the result is usable.
            first_start = self.period_start

        for _ in range(self.order_count):
            rack = self.racks[int(self.rng.integers(0, len(self.racks)))]
            requirements = self._requirements.get(rack, {})
            if not requirements:
                continue

            order_number = self._numbers.next()
            span = max((last_start - first_start).days, 1)
            start = first_start + dt.timedelta(days=int(self.rng.integers(0, span)))
            duration = int(self.rng.integers(*ORDER_DURATION_DAYS))
            finish = min(start + dt.timedelta(days=duration), self.period_end)
            target = int(self.rng.integers(1, 9))

            orders.append(
                {
                    "order_number": order_number,
                    "material_number": rack,
                    "plant": self.plant,
                    "target_quantity": target,
                    "uom": self.uom,
                    "order_date": start - dt.timedelta(days=int(self.rng.integers(1, 10))),
                    "start_date": start,
                    "finish_date": finish,
                    # Status is set once confirmations are known, below.
                    "status": ORDER_STATUS_RELEASED,
                    "created_by": "PLAN0" + str(int(self.rng.integers(1, 4))),
                }
            )
            order_plan[order_number] = {
                "material_number": rack,
                "target_quantity": target,
                "start_date": start,
                "finish_date": finish,
            }

            # Components are issued to the order at start, following the BOM.
            for position, (component, per_unit) in enumerate(sorted(requirements.items()), start=1):
                consumption.append(
                    {
                        "order_number": order_number,
                        "consumption_item": position * 10,
                        "component_material_number": component,
                        "quantity_consumed": per_unit * target,
                        # Kept so the row can be rescaled exactly once the
                        # order quantity is derived from the MES volume. A
                        # division would be lossy for a quantity.
                        "_quantity_per_unit": per_unit,
                        "uom": self.uom,
                        "movement_type": MVT_ISSUE_TO_ORDER,
                        "posting_date": start,
                        "entered_by": "WHSE0" + str(int(self.rng.integers(1, 4))),
                    }
                )

        return ProductionData(
            orders=orders,
            consumption=consumption,
            confirmations=[],
            order_plan=order_plan,
        )

    @staticmethod
    def rescale_consumption(consumption: list[dict[str, Any]], units_per_order: int) -> None:
        """Rescale consumption after the order quantity is derived from MES volume.

        Order quantities are not known until the MES event target is known, and
        consumption is the exploded requirement times the order quantity. Left
        unscaled, the two would disagree and the Phase 3 variance metrics would
        be measuring the generator rather than the business.
        """
        for row in consumption:
            per_unit = row.pop("_quantity_per_unit", None)
            if per_unit is None:
                continue
            row["quantity_consumed"] = per_unit * units_per_order

    def confirm(
        self,
        orders: Sequence[dict[str, Any]],
        completed_by_order: dict[str, int],
        scrapped_by_order: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Confirmations derived from the MES outcome.

        Generated *from* what MES recorded rather than independently, so with
        defects off CTRL-005 (MES versus ERP completion) reconciles exactly.
        Generating both sides independently would leave a gap that is noise,
        and a control whose baseline is noise cannot detect a real finding.
        """
        confirmations: list[dict[str, Any]] = []
        numbers = ids.confirmation_numbers()

        for order in orders:
            order_number = order["order_number"]
            confirmed = completed_by_order.get(order_number, 0)
            scrapped = scrapped_by_order.get(order_number, 0)
            if confirmed == 0 and scrapped == 0:
                continue
            confirmations.append(
                {
                    "order_number": order_number,
                    "confirmation_number": numbers.next(),
                    "confirmed_quantity": confirmed,
                    "scrap_quantity": scrapped,
                    "uom": self.uom,
                    "confirmation_date": order["finish_date"],
                    "posting_date": order["finish_date"],
                    "entered_by": "SHOP0" + str(int(self.rng.integers(1, 4))),
                }
            )
            order["status"] = (
                ORDER_STATUS_TECHNICALLY_COMPLETE
                if confirmed >= order["target_quantity"]
                else ORDER_STATUS_CONFIRMED
            )
        return confirmations
