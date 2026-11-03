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

from generator import bom, defects, inventory, invoices, production
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
    "supplier_invoice_headers": invoices.EXTRACT_SPECS["supplier_invoice_headers"],
    "supplier_invoice_lines": invoices.EXTRACT_SPECS["supplier_invoice_lines"],
    "production_orders": production.EXTRACT_SPECS["production_orders"],
    "component_consumption": production.EXTRACT_SPECS["component_consumption"],
    "inventory_revaluations": inventory.EXTRACT_SPECS["inventory_revaluations"],
    "gl_postings": inventory.EXTRACT_SPECS["gl_postings"],
}

# `production_confirmations` is deliberately absent until BTB-33.
#
# A confirmation records how many units an order actually completed, and
# CTRL-005 compares that against the units MES says passed final test.
# Generating the two independently would leave a gap made of noise, and a
# control whose clean-run baseline is noise cannot detect a real finding. So
# confirmations are generated *from* the MES outcome, which means they arrive
# with MES. `production.ProductionGenerator.confirm()` is ready for it.

LOAD_ORDER: tuple[str, ...] = tuple(EXTRACT_SPECS)


@dataclasses.dataclass
class Dataset:
    """Every generated extract, plus what downstream generators need."""

    extracts: dict[str, list[dict[str, Any]]]
    #: The standard cost windows that valued the inventory data.
    cost_windows: dict[str, list[tuple[dt.date, dt.date, Any]]]
    #: Rack material -> the tray materials it contains, for production and MES.
    rack_configurations: dict[str, list[str]]
    #: Production order -> what it plans to build. MES generates serials per
    #: order, so it needs the material, quantity and date window.
    order_plan: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

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

    invoice_generator = invoices.InvoiceGenerator(
        purchase_order_headers=inventory_data.purchase_order_headers,
        purchase_order_lines=inventory_data.purchase_order_lines,
        suppliers=inventory_data.suppliers,
        goods_movements=inventory_data.goods_movements,
        rng=generator.rng,
        uom=config.domain["uom_default"],
        period_end=generator.end,
    )
    invoice_data = invoice_generator.generate()

    production_generator = production.ProductionGenerator(
        bom_headers=bom_data.headers,
        bom_items=bom_data.items,
        rack_configurations=bom_data.rack_configurations,
        rng=generator.rng,
        plant=inventory.PLANT,
        uom=config.domain["uom_default"],
        period_start=generator.start,
        period_end=generator.end,
        order_count=profile.production_orders,
    )
    production_data = production_generator.generate()

    # One mapping per source system, merged in LOAD_ORDER. Building the dict
    # by iterating LOAD_ORDER rather than by merging means an extract declared
    # in the order but produced by nobody raises here instead of silently
    # missing from the output.
    produced: dict[str, list[dict[str, Any]]] = {
        **inventory_data.as_mapping(),
        **bom_data.as_mapping(),
        **invoice_data.as_mapping(),
        **production_data.as_mapping(),
    }
    extracts: dict[str, list[dict[str, Any]]] = {}
    for name in LOAD_ORDER:
        if name not in produced:
            raise KeyError(f"{name} is in LOAD_ORDER but no generator produced it")
        extracts[name] = produced[name]

    dataset = Dataset(
        extracts=extracts,
        cost_windows=inventory_data.cost_windows,
        rack_configurations=bom_data.rack_configurations,
        order_plan=production_data.order_plan,
    )

    planted = (
        generator.inject_defects(inventory_data, list(enabled_defects)) if enabled_defects else []
    )
    return dataset, planted
