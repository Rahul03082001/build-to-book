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
import pathlib
from collections.abc import Sequence
from typing import Any

from generator import bom, defects, inventory, invoices, mes, production, shipments, writers
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
    # Before goods_movements: a receipt references the delivery it arrived on
    # (ADR-026), so the deliveries have to be present when the movements land.
    # Generation order is the reverse -- deliveries are derived from the
    # receipts -- which is why load order and generation order are separate
    # concerns.
    "shipment_headers": shipments.EXTRACT_SPECS["shipment_headers"],
    "shipment_lines": shipments.EXTRACT_SPECS["shipment_lines"],
    "goods_movements": inventory.EXTRACT_SPECS["goods_movements"],
    "supplier_invoice_headers": invoices.EXTRACT_SPECS["supplier_invoice_headers"],
    "supplier_invoice_lines": invoices.EXTRACT_SPECS["supplier_invoice_lines"],
    "production_orders": production.EXTRACT_SPECS["production_orders"],
    "component_consumption": production.EXTRACT_SPECS["component_consumption"],
    "mes_unit_events": mes.EXTRACT_SPECS["mes_unit_events"],
    "production_confirmations": production.EXTRACT_SPECS["production_confirmations"],
    "inventory_revaluations": inventory.EXTRACT_SPECS["inventory_revaluations"],
    "gl_postings": inventory.EXTRACT_SPECS["gl_postings"],
}

#: Extracts too large to hold in memory, written by streaming chunks
#: (ADR-024). `mes_unit_events` is 5,000,000 rows on the full profile, which
#: measured at ~929 bytes per dict would be about 4.3 GB.
STREAMED: frozenset[str] = frozenset({"mes_unit_events"})

#: Extracts that cannot be produced until a stream has been consumed.
#:
#: A production confirmation records how many units an order completed, which
#: is only known once MES has recorded the outcome. Generating it independently
#: would leave CTRL-005 comparing two numbers that differ by noise, and a
#: control whose clean-run baseline is noise cannot detect a real finding.
#:
#: LOAD_ORDER places `mes_unit_events` before `production_confirmations`, so a
#: writer iterating that order has the outcome by the time it needs it. A test
#: asserts the ordering, because reversing it would silently produce empty
#: confirmations.
DEFERRED: frozenset[str] = frozenset({"production_confirmations"})

#: Columns a generator emits as None and a LATER generator fills in.
#:
#: `goods_movements` is produced by the inventory generator, but
#: `inbound_delivery` can only be known once the shipments are derived from
#: those same receipts (ADR-026). The producing generator emits None so its
#: output still satisfies its declared spec, and the shipment generator
#: overwrites it.
#:
#: Declared here rather than left as knowledge inside two modules, because a
#: test comparing an extract generated alone against the same extract
#: generated in a full run has to know which columns are legitimately
#: different. Without this it would either fail (as it did) or be weakened to
#: ignore all differences.
STAMPED_COLUMNS: dict[str, tuple[str, ...]] = {
    "goods_movements": ("inbound_delivery",),
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
    #: Production order -> what it plans to build. MES generates serials per
    #: order, so it needs the material, quantity and date window.
    order_plan: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    #: Extract name -> a callable returning an iterator of row chunks. Held as
    #: a factory rather than an iterator so it can be consumed more than once
    #: -- a spent generator would silently write an empty file on a second
    #: pass.
    streams: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: What MES recorded, so confirmations derive from it rather than being
    #: invented alongside it (CTRL-005).
    mes_outcome: Any = None
    #: Extract name -> a callable producing its rows, valid only after the
    #: streams it depends on have been consumed.
    deferred: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_mapping(self) -> dict[str, list[dict[str, Any]]]:
        """The in-memory extracts only. Streamed ones are in `streams`."""
        return self.extracts

    def row_count(self) -> int:
        return sum(len(rows) for rows in self.extracts.values())


def write_all(
    data: Dataset,
    out_dir: pathlib.Path,
    generated_at: dt.datetime,
) -> list[writers.WrittenExtract]:
    """Write every extract in the dataset, in load order.

    The one place that knows how each kind of source is written: in-memory
    extracts directly, streamed ones in chunks, deferred ones only after the
    stream they derive from has been consumed. Iterated in LOAD_ORDER and not
    over `extracts`, because a deferred extract must come after its stream.

    Shared by the CLI and the test helpers deliberately. They each had their own
    copy, and the test copy wrote only `as_mapping()` -- so the moment MES
    became a stream, the helper silently stopped producing two extracts and the
    loader failed with a missing file. A second copy of this dispatch is a
    second thing to forget.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[writers.WrittenExtract] = []
    for name in LOAD_ORDER:
        spec = EXTRACT_SPECS[name]
        if name in STREAMED:
            result = writers.write_extract_streaming(
                spec=spec,
                chunks=data.streams[name](),
                out_dir=out_dir,
                generated_at=generated_at,
            )
        else:
            rows = data.deferred[name]() if name in DEFERRED else data.as_mapping()[name]
            result = writers.write_extract(
                spec=spec,
                rows=rows,
                out_dir=out_dir,
                generated_at=generated_at,
            )
        written.append(result)
    return written


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

    # Order matters, and it changed with ADR-027. Inventory used to be
    # generated first and completely; now its purchase quantities have to cover
    # what production will consume, and its goods issues ARE that consumption.
    # So: masters, then the bill of materials, then production, then back to
    # inventory for procurement and movements.
    #
    # The two slices are no longer independent. That is the point -- they
    # describe one physical flow rather than two unrelated ones.
    # Every master is drawn here, before anything downstream exists. The
    # generators below take their own draws from the same seeded source, so
    # drawing a master after them would make it depend on how much randomness
    # they happened to use -- which is exactly what happened when procurement
    # moved to the end of the run: the same cost window came out at 21,358.00
    # inside a full build and 32,396.00 on its own. They are memoised, so
    # `generate()` reuses these rather than redrawing.
    materials = generator.materials()
    generator.suppliers()
    generator.standard_costs(materials)

    bom_generator = bom.BomGenerator(
        materials=materials,
        rng=generator.rng,
        plant=inventory.PLANT,
        uom=config.domain["uom_default"],
        period_start=generator.start,
        rack_configuration_count=profile.rack_configurations,
    )
    bom_data = bom_generator.generate()

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

    # MES events are generated per unit, so the order quantities have to imply
    # the profile's declared event count. They used to be randint(1, 9), which
    # produced ~158,000 events against a declared 5,000,000 on the full
    # profile -- the two config numbers disagreed by a factor of 32 and
    # nothing checked.
    units_per_order = mes.order_quantity_for_profile(
        declared_events=profile.mes_events,
        order_count=profile.production_orders,
        step_count=len(config.domain["mes_steps"]),
        max_rework_attempts=int(config.domain["max_rework_attempts"]),
        step_failure_rates=[
            float(step.get("failure_rate", 0.0)) for step in config.domain["mes_steps"]
        ],
    )
    for order in production_data.orders:
        order["target_quantity"] = units_per_order
    for plan in production_data.order_plan.values():
        plan["target_quantity"] = units_per_order
    # Consumption follows the BOM times the order quantity, so it has to be
    # rescaled with it or the Phase 3 variance metrics measure the generator.
    production_generator.rescale_consumption(production_data.consumption, units_per_order)

    # Demand: what production will consume of each component, after the
    # rescale above. Purchase quantities are sized from this (ADR-027), so
    # receipts cover consumption instead of being drawn from a range that had
    # no relationship to it.
    #
    # Computed here rather than inside the inventory generator because it is a
    # fact about production, and putting it there would mean the inventory
    # slice deciding how much the factory needs.
    demand: dict[str, int] = {}
    for line in production_data.consumption:
        component = line["component_material_number"]
        demand[component] = demand.get(component, 0) + int(line["quantity_consumed"])

    # Procurement and the material ledger, now that demand is known. The goods
    # issues in here ARE production_data.consumption.
    inventory_data = generator.generate(demand=demand, consumption=production_data.consumption)

    # Refuses a negative balance rather than clamping the issues. Demand is
    # fixed by the bill of materials, so stock running out is a configuration
    # fault that must stop the run, not something to quietly absorb.
    inventory.assert_on_hand_never_negative(inventory_data.goods_movements)

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

    # Shipments come from the receipts that already exist, and stamp
    # `inbound_delivery` onto those movement rows in place (ADR-026). Derived
    # rather than generated independently so shipped quantity ties to received
    # quantity exactly, and so there is no second row count to disagree with
    # the first -- the fault `mes.order_quantity_for_profile` exists to prevent.
    shipment_outcome = shipments.build(
        config=config,
        movements=inventory_data.goods_movements,
        po_headers=inventory_data.purchase_order_headers,
        po_lines=inventory_data.purchase_order_lines,
        period_start=generator.start,
        period_end=generator.end,
    )

    # One mapping per source system, merged in LOAD_ORDER. Building the dict
    # by iterating LOAD_ORDER rather than by merging means an extract declared
    # in the order but produced by nobody raises here instead of silently
    # missing from the output.
    mes_generator = mes.MesGenerator(
        order_plan=production_data.order_plan,
        steps=config.domain["mes_steps"],
        failure_codes=config.domain["failure_codes"],
        max_rework_attempts=int(config.domain["max_rework_attempts"]),
        # Its own seed, offset from the run's, so the stream is reproducible
        # across repeated consumption and independent of how many draws the
        # earlier generators made.
        seed=config.seed + 1,
        period_end=generator.end,
    )

    produced: dict[str, list[dict[str, Any]]] = {
        "shipment_headers": shipment_outcome.headers,
        "shipment_lines": shipment_outcome.lines,
        **inventory_data.as_mapping(),
        **bom_data.as_mapping(),
        **invoice_data.as_mapping(),
        **production_data.as_mapping(),
    }
    streams: dict[str, Any] = {"mes_unit_events": mes_generator.stream}

    deferred: dict[str, Any] = {
        "production_confirmations": lambda: production_generator.confirm(
            production_data.orders,
            mes_generator.outcome.completed_by_order,
            mes_generator.outcome.scrapped_by_order,
        )
    }

    extracts: dict[str, list[dict[str, Any]]] = {}
    for name in LOAD_ORDER:
        if name in STREAMED:
            if name not in streams:
                raise KeyError(f"{name} is declared streamed but has no stream")
            continue
        if name in DEFERRED:
            if name not in deferred:
                raise KeyError(f"{name} is declared deferred but has no producer")
            continue
        if name not in produced:
            raise KeyError(f"{name} is in LOAD_ORDER but no generator produced it")
        extracts[name] = produced[name]

    dataset = Dataset(
        extracts=extracts,
        cost_windows=inventory_data.cost_windows,
        rack_configurations=bom_data.rack_configurations,
        order_plan=production_data.order_plan,
        streams=streams,
        mes_outcome=mes_generator.outcome,
        deferred=deferred,
    )

    planted = (
        generator.inject_defects(inventory_data, list(enabled_defects)) if enabled_defects else []
    )
    return dataset, planted
