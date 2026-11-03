"""Multi-level bills of materials (BTB-30).

Shaped on SAP's BOM header and item tables (STKO and STPO), per ADR-002: a
header describes the BOM for one parent material, and its items list the
components. Multi-level structure is emergent rather than declared -- a
component that is itself a parent has its own header, so depth comes from the
data instead of from a `level` column somebody has to keep correct.

The hierarchy mirrors how a rack is actually built:

    RACK_ASSY  (level 0)  a rack configuration
      CHASSIS  (level 1)  trays: GPU tray, compute tray
        GPU, CPU, DIMM, NVME, PSU   (level 2)  components
      NIC, CABLE           (level 1)  fitted directly to the rack

Nothing here explodes the BOM. That happens in `int_bom_exploded` with a
recursive CTE (ADR-025), so the transformation that decides every component
requirement figure is reviewable SQL rather than Python.

BOMs are built as a strict hierarchy, so a cycle cannot occur. A dbt test
asserts acyclicity against the raw data anyway -- trusting the generator is
exactly the assumption a planted defect would break later.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from generator import ids
from generator.writers import ExtractSpec

#: SAP BOM usage codes. 1 is production, which is the only one modelled.
BOM_USAGE_PRODUCTION = "1"

#: Status 01 means released for production.
BOM_STATUS_RELEASED = "01"

#: Component classes fitted inside a tray.
TRAY_COMPONENT_GROUPS = ("GPU", "CPU", "DIMM", "NVME", "PSU")

#: Component classes fitted directly to the rack, outside a tray.
RACK_DIRECT_GROUPS = ("NIC", "CABLE")

#: Trays per rack configuration, and components per tray. Kept small enough
#: that a reviewer can hand-check an explosion, large enough that the
#: multiplication down two levels is not trivially 1 x 1.
TRAYS_PER_RACK = (2, 4)
COMPONENTS_PER_TRAY = (3, 6)
DIRECT_COMPONENTS_PER_RACK = (1, 3)

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "bom_headers": ExtractSpec(
        name="bom_headers",
        columns=[
            "bom_number",
            "material_number",
            "plant",
            "bom_usage",
            "base_quantity",
            "uom",
            "valid_from",
            "valid_to",
            "status",
            "created_by",
        ],
        source_system="ERP",
        value_column=None,
        natural_key=["bom_number"],
    ),
    "bom_items": ExtractSpec(
        name="bom_items",
        columns=[
            "bom_number",
            "bom_item",
            "component_material_number",
            "quantity_per",
            "uom",
            "is_phantom",
        ],
        source_system="ERP",
        # Quantities are per base quantity of the parent, not money.
        value_column=None,
        natural_key=["bom_number", "bom_item"],
    ),
}


@dataclasses.dataclass
class BomData:
    """Generated BOM extracts, plus the structure for downstream generators."""

    headers: list[dict[str, Any]]
    items: list[dict[str, Any]]

    #: rack material -> the tray materials it contains. Production orders and
    #: MES events are generated per rack configuration, so they need this.
    rack_configurations: dict[str, list[str]]

    def as_mapping(self) -> dict[str, list[dict[str, Any]]]:
        return {"bom_headers": self.headers, "bom_items": self.items}


class BomGenerator:
    """Builds parent-child BOMs over an existing material master."""

    def __init__(
        self,
        materials: Sequence[dict[str, Any]],
        rng: Any,
        plant: str,
        uom: str,
        period_start: dt.date,
        rack_configuration_count: int,
    ) -> None:
        self.rng = rng
        self.plant = plant
        self.uom = uom
        self.period_start = period_start
        self.rack_configuration_count = rack_configuration_count
        self._bom_numbers = ids.bom_numbers()

        self._materials = list(materials)
        by_group: dict[str, list[dict[str, Any]]] = {}
        for material in materials:
            by_group.setdefault(material["material_group"], []).append(material)
        self._by_group = by_group

    def _pick(self, group: str, count: int) -> list[dict[str, Any]]:
        """Distinct materials from a group, deterministically."""
        pool = self._by_group.get(group, [])
        if not pool:
            return []
        count = min(count, len(pool))
        indices = self.rng.choice(len(pool), size=count, replace=False)
        return [pool[int(i)] for i in sorted(indices)]

    def _pick_from(self, numbers: Sequence[str], count: int) -> list[dict[str, Any]]:
        """Distinct materials chosen from an explicit list of material numbers.

        Used where the choice has to come from a prepared subset -- the trays
        that have their own bill of materials -- rather than from a whole
        material group.
        """
        pool = [m for m in self._materials if m["material_number"] in set(numbers)]
        if not pool:
            return []
        count = min(count, len(pool))
        indices = self.rng.choice(len(pool), size=count, replace=False)
        return [pool[int(i)] for i in sorted(indices)]

    def _between(self, low: int, high: int) -> int:
        return int(self.rng.integers(low, high + 1))

    def generate(self) -> BomData:
        headers: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        rack_configurations: dict[str, list[str]] = {}

        racks = self._pick("RACK_ASSY", self.rack_configuration_count)
        if not racks:
            return BomData(headers=headers, items=items, rack_configurations={})

        # BOMs are valid from before the reporting window, so every production
        # order in the window resolves against a released BOM.
        valid_from = self.period_start - dt.timedelta(days=90)
        far_future = dt.date(9999, 12, 31)

        # Trays are shared across racks, as they are in a real product family:
        # the same GPU tray goes into several rack configurations. That makes
        # the explosion non-trivial -- a component can be reached by more than
        # one path and the quantities must add up.
        tray_pool = self._pick("CHASSIS", max(2, self.rack_configuration_count))
        tray_boms: dict[str, str] = {}

        for tray in tray_pool:
            bom_number = self._bom_numbers.next()
            tray_boms[tray["material_number"]] = bom_number
            headers.append(
                {
                    "bom_number": bom_number,
                    "material_number": tray["material_number"],
                    "plant": self.plant,
                    "bom_usage": BOM_USAGE_PRODUCTION,
                    "base_quantity": 1,
                    "uom": self.uom,
                    "valid_from": valid_from,
                    "valid_to": far_future,
                    "status": BOM_STATUS_RELEASED,
                    "created_by": "ENG001",
                }
            )

            chosen_groups = [group for group in TRAY_COMPONENT_GROUPS if self._by_group.get(group)][
                : self._between(*COMPONENTS_PER_TRAY)
            ]
            for position, group in enumerate(chosen_groups, start=1):
                component = self._pick(group, 1)
                if not component:
                    continue
                items.append(
                    {
                        "bom_number": bom_number,
                        "bom_item": position * 10,
                        "component_material_number": component[0]["material_number"],
                        # GPUs come in multiples per tray; a PSU does not.
                        "quantity_per": self._between(1, 8)
                        if group in ("GPU", "DIMM", "NVME")
                        else 1,
                        "uom": self.uom,
                        "is_phantom": False,
                    }
                )

        for rack in racks:
            bom_number = self._bom_numbers.next()
            headers.append(
                {
                    "bom_number": bom_number,
                    "material_number": rack["material_number"],
                    "plant": self.plant,
                    "bom_usage": BOM_USAGE_PRODUCTION,
                    "base_quantity": 1,
                    "uom": self.uom,
                    "valid_from": valid_from,
                    "valid_to": far_future,
                    "status": BOM_STATUS_RELEASED,
                    "created_by": "ENG001",
                }
            )

            position = 0
            # Chosen from the trays that HAVE a bill of materials, not from the
            # whole CHASSIS group.
            #
            # Picking from the group meant a rack could draw only trays outside
            # `tray_pool`; each was then skipped as a dead end and the rack came
            # out with no trays at all -- unbuildable, and silently so, because
            # nothing downstream asked whether a rack configuration had any
            # content. Latent from the start; it first fired when ADR-027
            # reordered generation and the draws fell differently.
            trays_in_rack = self._pick_from(list(tray_boms), self._between(*TRAYS_PER_RACK))
            tray_numbers: list[str] = []
            for tray in trays_in_rack:
                if tray["material_number"] not in tray_boms:
                    # Unreachable now that the pick is drawn from tray_boms.
                    # Kept as a guard: a future change to the pool should fail
                    # a test rather than emit a leaf posing as a sub-assembly.
                    continue
                position += 1
                tray_numbers.append(tray["material_number"])
                items.append(
                    {
                        "bom_number": bom_number,
                        "bom_item": position * 10,
                        "component_material_number": tray["material_number"],
                        "quantity_per": self._between(1, 4),
                        "uom": self.uom,
                        "is_phantom": False,
                    }
                )

            for group in RACK_DIRECT_GROUPS:
                for component in self._pick(group, self._between(*DIRECT_COMPONENTS_PER_RACK)):
                    position += 1
                    items.append(
                        {
                            "bom_number": bom_number,
                            "bom_item": position * 10,
                            "component_material_number": component["material_number"],
                            "quantity_per": self._between(1, 12),
                            "uom": self.uom,
                            "is_phantom": False,
                        }
                    )

            rack_configurations[rack["material_number"]] = tray_numbers

        return BomData(headers=headers, items=items, rack_configurations=rack_configurations)
