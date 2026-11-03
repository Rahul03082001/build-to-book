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

from generator import (
    accounts,
    bom,
    costing,
    defects,
    inventory,
    invoices,
    mes,
    production,
    shipments,
    writers,
)
from generator.config import GeneratorConfig
from generator.writers import ExtractSpec

#: Every extract, in load order: masters before the documents that reference
#: them, so a foreign-key problem surfaces against data already present.
EXTRACT_SPECS: dict[str, ExtractSpec] = {
    # Master data from finance, first: every posting references an account, so
    # the chart has to be present before any document that names one.
    "chart_of_accounts": accounts.EXTRACT_SPECS["chart_of_accounts"],
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
    "supplier_invoice_headers": invoices.EXTRACT_SPECS["supplier_invoice_headers"],
    "supplier_invoice_lines": invoices.EXTRACT_SPECS["supplier_invoice_lines"],
    "production_orders": production.EXTRACT_SPECS["production_orders"],
    "component_consumption": production.EXTRACT_SPECS["component_consumption"],
    "mes_unit_events": mes.EXTRACT_SPECS["mes_unit_events"],
    "production_confirmations": production.EXTRACT_SPECS["production_confirmations"],
    # After the confirmations, because finished goods receipts come from them
    # (ADR-028) and a confirmation is only known once MES has recorded the
    # outcome. Load order and generation order diverge again: the movements
    # reference shipments, which are derived from the movements -- so shipments
    # load first and are generated from the raw-material part of the ledger,
    # which exists long before production confirms anything.
    "goods_movements": inventory.EXTRACT_SPECS["goods_movements"],
    "inventory_revaluations": inventory.EXTRACT_SPECS["inventory_revaluations"],
    # Last: every posting comes from a movement, a revaluation or an order's
    # costing, so the ledger cannot be written until all three exist.
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
DEFERRED: frozenset[str] = frozenset(
    {
        "production_confirmations",
        # Finished goods receipts and dispatches are material documents that
        # only exist once production has confirmed output, which needs the MES
        # outcome (ADR-028).
        "goods_movements",
        # And every posting needs the movements, so the ledger is last.
        "gl_postings",
    }
)

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
        """The in-memory extracts only. Streamed and deferred ones are not here.

        Deliberately partial. `goods_movements` and `gl_postings` became
        deferred with ADR-028 -- finished goods receipts cannot exist until MES
        has recorded the outcome -- so reading them from here raises KeyError
        rather than returning a stale copy. `materialise` is the way to get
        everything.
        """
        return self.extracts

    def materialise(self) -> dict[str, list[dict[str, Any]]]:
        """Every extract, with streams drained and deferred callables run.

        One obvious way to get the whole dataset, so no caller has to know
        which extracts are deferred or in what order they depend on each other.
        Iterated in LOAD_ORDER, which is what makes the dependencies resolve:
        the confirmations come from the MES outcome, the movements from the
        confirmations, and the ledger from the movements.

        Holds everything in memory, which is exactly what streaming exists to
        avoid (ADR-024) -- 5,000,000 MES events measured about 4.3 GB as dicts.
        So this is for tests and the demo profile. Anything that has to handle
        the full profile uses `write_all`, which streams.
        """
        materialised: dict[str, list[dict[str, Any]]] = {}
        for name in LOAD_ORDER:
            if name in STREAMED:
                materialised[name] = [row for chunk in self.streams[name]() for row in chunk]
            elif name in DEFERRED:
                materialised[name] = list(self.deferred[name]())
            else:
                materialised[name] = self.extracts[name]
        return materialised

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

    # A manufactured material's standard cost is the sum of what goes into it,
    # not an independent price. Rolled up now that the bills of materials
    # exist, and written back over whatever the master drew for racks and
    # trays -- rack 1000008 was carrying 8,461 against a BOM worth 1,161,502.
    #
    # It did not matter while production was invisible to finance. ADR-028 puts
    # finished goods on the balance sheet, and then the general ledger and
    # CTRL-003's quantity-times-cost invariant would compute inventory from two
    # different numbers.
    standard_cost_rows, cost_lookup = generator.standard_costs(materials)
    rolled = costing.roll_up(
        bom_headers=bom_data.headers,
        bom_items=bom_data.items,
        component_cost={
            row["material_number"]: row["standard_cost_usd"] for row in standard_cost_rows
        },
    )
    # Mutates both in place, so every later valuation -- movements, postings,
    # the inventory balance -- reads the same number. Two sources of truth for
    # a standard cost is how the ledger and the quantity-times-cost invariant
    # come to disagree.
    costing.apply_to_standard_costs(
        standard_costs=standard_cost_rows,
        rolled=rolled,
        cost_lookup=cost_lookup,
        period_start=generator.start,
        currency=config.domain["currency"],
        approved_by="COSTING",
    )

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
    # Materials the factory builds rather than buys, so purchasing excludes
    # them (see `purchase_orders`).
    manufactured = frozenset(header["material_number"] for header in bom_data.headers)

    inventory_data = generator.generate(
        demand=demand,
        consumption=production_data.consumption,
        manufactured=manufactured,
    )

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
        # Static master data: a chart of accounts is maintained by finance, not
        # invented by a run.
        "chart_of_accounts": accounts.rows(),
        "shipment_headers": shipment_outcome.headers,
        "shipment_lines": shipment_outcome.lines,
        **inventory_data.as_mapping(),
        **bom_data.as_mapping(),
        **invoice_data.as_mapping(),
        **production_data.as_mapping(),
    }
    streams: dict[str, Any] = {"mes_unit_events": mes_generator.stream}

    # The standard-costing chain (ADR-028). All three are memoised, because the
    # writer calls each deferred extract once but they depend on one another:
    # the movements need the confirmations, and the postings need both. Calling
    # `confirm` twice would also allocate two sets of confirmation numbers for
    # the same output.
    chain: dict[str, Any] = {}
    # Appended to by build_ledger at write time, and returned to the caller as
    # the same object -- so the manifest must be populated AFTER the extracts
    # are written, not before.
    planted: list[defects.InjectedRecord] = []

    def confirmations() -> list[dict[str, Any]]:
        if "confirmations" not in chain:
            chain["confirmations"] = production_generator.confirm(
                production_data.orders,
                mes_generator.outcome.completed_by_order,
                mes_generator.outcome.scrapped_by_order,
            )
        return chain["confirmations"]

    def build_ledger() -> None:
        """Build the final movements and postings, then plant any defects.

        Both in one step, and defects planted here, because `goods_movements`
        is written before `gl_postings` and a defect has to be in BOTH lists
        before either reaches a file. Planting into the inventory slice's own
        lists instead silently lost `backdated_gl_posting` entirely: the
        postings are regenerated here from movements that did not exist when
        the slice was made, so the injected document was discarded and the
        manifest claimed a defect the extract did not contain.
        """
        if "movements" in chain:
            return

        receipts, dispatches = costing.production_movements(
            confirmations=confirmations(),
            order_plan=production_data.order_plan,
            unit_cost=rolled,
            next_document=generator.next_material_document,
            clerk_for=generator.clerk,
            dispatch_rate=float(config.domain["finished_goods_dispatch_rate"]),
            period_end=generator.end,
            rng=generator.rng,
            plant=inventory.PLANT,
            uom=config.domain["uom_default"],
        )
        movements = [*inventory_data.goods_movements, *receipts, *dispatches]

        # What each order absorbed, from the issues actually posted rather than
        # from the bill of materials -- taking both sides of a variance from the
        # standard would guarantee zero.
        issue_value_by_order: dict[str, Any] = {}
        for movement in inventory_data.goods_movements:
            order_number = movement.get("order_number")
            if movement["movement_type"] != inventory.MVT_GOODS_ISSUE or not order_number:
                continue
            issue_value_by_order[order_number] = issue_value_by_order.get(order_number, 0) + abs(
                movement["amount_usd"]
            )

        postings = generator.gl_postings(
            movements,
            inventory_data.inventory_revaluations,
            costing.order_costings(
                confirmations=confirmations(),
                order_plan=production_data.order_plan,
                unit_cost=rolled,
                issue_value_by_order=issue_value_by_order,
            ),
        )

        if enabled_defects:
            planted.extend(
                generator.inject_defects(
                    enabled=list(enabled_defects),
                    movements=movements,
                    postings=postings,
                )
            )

        chain["movements"] = movements
        chain["postings"] = postings

    def all_movements() -> list[dict[str, Any]]:
        """Raw-material movements plus the finished goods the factory produced."""
        build_ledger()
        return chain["movements"]

    def postings() -> list[dict[str, Any]]:
        build_ledger()
        return chain["postings"]

    deferred: dict[str, Any] = {
        "production_confirmations": confirmations,
        "goods_movements": all_movements,
        "gl_postings": postings,
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

    # `planted` is filled when the ledger is built, which happens while the
    # extracts are being written (see build_ledger). It is returned as the same
    # list object, so a caller that reads it before writing sees nothing --
    # which is why the CLI and the test helper populate the manifest afterwards.
    return dataset, planted
