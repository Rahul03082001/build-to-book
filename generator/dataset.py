"""Compose the source-system generators into one dataset.

`inventory.py` was approaching a thousand lines before Phase 2 added four more
source systems, so each system gets its own module and this one composes them.
The composition is explicit rather than discovered: adding a source means
adding it here, which keeps the extract list, the load order and the defect
injection points in one readable place.

Generation order matters and is not alphabetical. Master data comes first,
then the BOMs that reference it, then the documents that reference both --
because a generator that needs a material number has to be able to ask for one
that exists.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from generator import bom, defects, inventory
from generator.config import GeneratorConfig
from generator.writers import ExtractSpec

#: Every extract, in load order: masters before the documents that reference
#: them, so a foreign-key problem surfaces against data already present.
EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "materials": inventory.EXTRACT_SPECS["materials"],
    "suppliers": inventory.EXTRACT_SPECS["suppliers"],
    "standard_costs": inventory.EXTRACT_SPECS["standard_costs"],
    "bom_headers": bom.EXTRACT_SPECS["bom_headers"],
    "bom_items": bom.EXTRACT_SPECS["bom_items"],
    "purchase_order_headers": inventory.EXTRACT_SPECS["purchase_order_headers"],
    "purchase_order_lines": inventory.EXTRACT_SPECS["purchase_order_lines"],
    "goods_movements": inventory.EXTRACT_SPECS["goods_movements"],
    "inventory_revaluations": inventory.EXTRACT_SPECS["inventory_revaluations"],
    "gl_postings": inventory.EXTRACT_SPECS["gl_postings"],
}

LOAD_ORDER: tuple[str, ...] = tuple(EXTRACT_SPECS)


@dataclasses.dataclass
class Dataset:
    """Every generated extract, plus what downstream generators need."""

    extracts: dict[str, list[dict[str, Any]]]
    #: The standard cost windows that valued the inventory data.
    cost_windows: dict[str, list[tuple[dt.date, dt.date, Any]]]
    #: Rack material -> the tray materials it contains, for production and MES.
    rack_configurations: dict[str, list[str]]

    def as_mapping(self) -> dict[str, list[dict[str, Any]]]:
        return self.extracts

    def row_count(self) -> int:
        return sum(len(rows) for rows in self.extracts.values())


def build(
    config: GeneratorConfig,
    profile_name: str,
    enabled_defects: Sequence[str] = (),
) -> tuple[Dataset, list[defects.InjectedRecord]]:
    """Generate every source extract, then plant any requested defects.

    Defects are injected after the clean data is complete and internally
    consistent, so a defect is a deliberate deviation from a known-good
    baseline rather than an artefact of generation order.

    Args:
        config: The generator config.
        profile_name: ``demo`` or ``full``.
        enabled_defects: Defect keys to plant. Empty means a clean run.

    Returns:
        The dataset, and the records that were planted.
    """
    profile = config.profile(profile_name)
    generator = inventory.InventoryGenerator(config, profile_name)

    inventory_data = generator.generate()

    bom_generator = bom.BomGenerator(
        materials=inventory_data.materials,
        rng=generator.rng,
        plant=inventory.PLANT,
        uom=config.domain["uom_default"],
        period_start=generator.start,
        rack_configuration_count=profile.rack_configurations,
    )
    bom_data = bom_generator.generate()

    extracts: dict[str, list[dict[str, Any]]] = {}
    inventory_mapping = inventory_data.as_mapping()
    bom_mapping = bom_data.as_mapping()
    for name in LOAD_ORDER:
        if name in inventory_mapping:
            extracts[name] = inventory_mapping[name]
        elif name in bom_mapping:
            extracts[name] = bom_mapping[name]
        else:  # pragma: no cover - guarded by the test below
            raise KeyError(f"{name} is in LOAD_ORDER but no generator produced it")

    dataset = Dataset(
        extracts=extracts,
        cost_windows=inventory_data.cost_windows,
        rack_configurations=bom_data.rack_configurations,
    )

    planted = (
        generator.inject_defects(inventory_data, list(enabled_defects)) if enabled_defects else []
    )
    return dataset, planted
