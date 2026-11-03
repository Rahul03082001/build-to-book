"""The dataset composition and the streaming writer.

Phase 2 adds four source systems to a generator that had one, so the things
most likely to go wrong are not the data but the wiring: an extract that
generates and never loads, a schema that was never declared, a load order that
drifted from the generator's. Each of those failed silently at least once
while this was being built, so each has a test.
"""

from __future__ import annotations

import datetime as dt
import decimal
import filecmp
import json
import pathlib
import resource
from typing import Any

import pytest

from generator import bom, dataset, inventory, writers
from generator import config as genconfig
from ingestion import schemas

D = decimal.Decimal
STAMP = dt.datetime(2026, 11, 2, 12, 0, 0)


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


@pytest.fixture(scope="module")
def built(cfg: genconfig.GeneratorConfig) -> tuple[dataset.Dataset, list]:
    return dataset.build(cfg, "demo")


# ---------------------------------------------------------------------------
# Wiring: the failures that are silent
# ---------------------------------------------------------------------------
def test_load_order_covers_every_spec() -> None:
    assert set(dataset.LOAD_ORDER) == set(dataset.EXTRACT_SPECS)


def test_every_extract_in_load_order_is_actually_generated(
    built: tuple[dataset.Dataset, list],
) -> None:
    """An extract that is declared but never produced loads as a missing file.

    Every name in LOAD_ORDER must be produced by exactly one of the three
    mechanisms -- in memory, streamed, or deferred until the stream it derives
    from has been consumed. A name in none of them is a missing file at load
    time; a name in two is ambiguous about which copy wins.
    """
    data, _ = built
    in_memory = set(data.as_mapping())
    streamed = set(data.streams)
    deferred = set(data.deferred)

    produced = in_memory | streamed | deferred
    assert produced == set(dataset.LOAD_ORDER), (
        f"declared but not produced: {sorted(set(dataset.LOAD_ORDER) - produced)}; "
        f"produced but not declared: {sorted(produced - set(dataset.LOAD_ORDER))}"
    )
    assert streamed == dataset.STREAMED
    assert deferred == dataset.DEFERRED
    for pair in ((in_memory, streamed), (in_memory, deferred), (streamed, deferred)):
        assert not pair[0] & pair[1], f"produced two ways: {sorted(pair[0] & pair[1])}"

    for name, rows in data.as_mapping().items():
        assert rows, f"{name} generated zero rows"


def test_every_extract_has_a_declared_schema() -> None:
    """The drift that bit three times: generate, then fail to load.

    A new extract needs a spec, a pandera schema, a natural key, a raw table
    and a dbt source. Four of those are checked here and in the structure
    tests; the fifth fails the dbt build loudly.
    """
    missing = set(dataset.LOAD_ORDER) - set(schemas.SCHEMAS)
    assert not missing, f"extracts with no pandera schema: {sorted(missing)}"


def test_every_extract_has_a_declared_natural_key() -> None:
    missing = set(dataset.LOAD_ORDER) - set(schemas.NATURAL_KEYS)
    assert not missing, f"extracts with no natural key: {sorted(missing)}"


def test_schema_columns_match_the_spec_columns() -> None:
    for name in dataset.LOAD_ORDER:
        spec_columns = set(dataset.EXTRACT_SPECS[name].columns)
        schema_columns = set(schemas.SCHEMAS[name].columns)
        assert spec_columns == schema_columns, (
            f"{name}: spec has {spec_columns ^ schema_columns} that the schema does not"
        )


def test_natural_keys_agree_between_spec_and_loader() -> None:
    """The sidecar's claim and the loader's contract must match (ADR-020)."""
    for name in dataset.LOAD_ORDER:
        assert tuple(dataset.EXTRACT_SPECS[name].natural_key) == schemas.natural_key_for(name), name


def test_raw_ddl_declares_every_extract() -> None:
    """A missing raw table is only discovered at load time."""
    ddl = (
        pathlib.Path(__file__).resolve().parent.parent / "warehouse" / "sql" / "raw.sql"
    ).read_text()
    for name in dataset.LOAD_ORDER:
        assert f"raw.{name} (" in ddl, f"raw.sql has no table for {name}"


def test_dbt_sources_declare_every_extract() -> None:
    sources = (
        pathlib.Path(__file__).resolve().parent.parent
        / "warehouse"
        / "dbt"
        / "models"
        / "staging"
        / "_sources.yml"
    ).read_text()
    for name in dataset.LOAD_ORDER:
        assert f"- name: {name}" in sources, f"_sources.yml has no source for {name}"


def test_masters_load_before_the_documents_that_reference_them() -> None:
    order = list(dataset.LOAD_ORDER)
    assert order.index("materials") < order.index("bom_items")
    assert order.index("bom_headers") < order.index("bom_items")
    assert order.index("purchase_order_headers") < order.index("purchase_order_lines")
    assert order.index("goods_movements") < order.index("gl_postings")


# ---------------------------------------------------------------------------
# BTB-30: the BOM structure
# ---------------------------------------------------------------------------
def test_bom_is_genuinely_multi_level(built: tuple[dataset.Dataset, list]) -> None:
    """A single-level BOM would make the recursive explosion pointless."""
    data, _ = built
    headers = {h["material_number"]: h["bom_number"] for h in data.as_mapping()["bom_headers"]}
    items = data.as_mapping()["bom_items"]

    # A component that is itself a parent is a sub-assembly.
    sub_assemblies = {
        i["component_material_number"] for i in items if i["component_material_number"] in headers
    }
    assert sub_assemblies, "no component has a BOM of its own, so the BOM is flat"


def test_bom_items_reference_a_real_header(built: tuple[dataset.Dataset, list]) -> None:
    data, _ = built
    numbers = {h["bom_number"] for h in data.as_mapping()["bom_headers"]}
    orphans = [i for i in data.as_mapping()["bom_items"] if i["bom_number"] not in numbers]
    assert not orphans, f"{len(orphans)} BOM items have no header"


def test_bom_components_are_real_materials(built: tuple[dataset.Dataset, list]) -> None:
    data, _ = built
    materials = {m["material_number"] for m in data.as_mapping()["materials"]}
    for item in data.as_mapping()["bom_items"]:
        assert item["component_material_number"] in materials, item


def test_bom_has_no_cycles(built: tuple[dataset.Dataset, list]) -> None:
    """Asserted in Python as well as in dbt.

    The dbt test checks the warehouse; this checks the generator, so a cycle
    cannot be introduced into the data and only noticed after a load.
    """
    data, _ = built
    headers = {h["bom_number"]: h["material_number"] for h in data.as_mapping()["bom_headers"]}
    children: dict[str, list[str]] = {}
    for item in data.as_mapping()["bom_items"]:
        parent = headers[item["bom_number"]]
        children.setdefault(parent, []).append(item["component_material_number"])

    def walk(node: str, seen: tuple[str, ...]) -> None:
        if node in seen:
            raise AssertionError(f"cycle: {' -> '.join([*seen, node])}")
        for child in children.get(node, []):
            walk(child, (*seen, node))

    for root in children:
        walk(root, ())


def test_a_tray_is_shared_across_rack_configurations(
    built: tuple[dataset.Dataset, list],
) -> None:
    """Shared sub-assemblies are what make the explosion non-trivial.

    If every tray belonged to one rack, every component would be reachable by
    exactly one path and a wrong `group by` in the explosion would be
    invisible.
    """
    data, _ = built
    headers = {h["material_number"]: h["bom_number"] for h in data.as_mapping()["bom_headers"]}
    usage: dict[str, int] = {}
    for item in data.as_mapping()["bom_items"]:
        component = item["component_material_number"]
        if component in headers:
            usage[component] = usage.get(component, 0) + 1
    assert any(count > 1 for count in usage.values()), (
        "no sub-assembly is used by more than one parent"
    )


def test_rack_configurations_are_exposed_for_downstream_generators(
    cfg: genconfig.GeneratorConfig, built: tuple[dataset.Dataset, list]
) -> None:
    """Production orders and MES events are generated per rack configuration."""
    data, _ = built
    assert len(data.rack_configurations) == cfg.profile("demo").rack_configurations
    assert all(trays for trays in data.rack_configurations.values()), (
        "a rack configuration with no trays cannot be built"
    )


def test_bom_quantities_are_positive(built: tuple[dataset.Dataset, list]) -> None:
    data, _ = built
    for item in data.as_mapping()["bom_items"]:
        assert item["quantity_per"] > 0, item
    for header in data.as_mapping()["bom_headers"]:
        assert header["base_quantity"] > 0, header


def test_boms_are_valid_before_the_reporting_window(
    cfg: genconfig.GeneratorConfig, built: tuple[dataset.Dataset, list]
) -> None:
    """A production order inside the window must resolve against a released BOM."""
    data, _ = built
    generator = inventory.InventoryGenerator(cfg, "demo")
    for header in data.as_mapping()["bom_headers"]:
        assert header["valid_from"] < generator.start
        assert header["valid_to"] > generator.end
        assert header["status"] == bom.BOM_STATUS_RELEASED


# ---------------------------------------------------------------------------
# ADR-024: the streaming writer
# ---------------------------------------------------------------------------
def test_streaming_and_in_memory_writers_agree(
    built: tuple[dataset.Dataset, list], tmp_path: pathlib.Path
) -> None:
    """Two write paths must not produce different bytes for the same data.

    `write_extract` delegates to the streaming writer precisely so they
    cannot, and this pins it: if someone reimplements the in-memory path for
    speed, the files have to stay identical.
    """
    data, _ = built
    spec = dataset.EXTRACT_SPECS["goods_movements"]
    rows = data.materialise()["goods_movements"]

    one = writers.write_extract(spec, rows, tmp_path / "memory", STAMP)
    chunks = (rows[i : i + 500] for i in range(0, len(rows), 500))
    two = writers.write_extract_streaming(spec, chunks, tmp_path / "stream", STAMP)

    assert one.row_count == two.row_count
    assert one.value_total == two.value_total
    assert one.sha256 == two.sha256
    assert filecmp.cmp(one.path, two.path, shallow=False)
    assert json.loads(one.control_totals_path.read_text()) == json.loads(
        two.control_totals_path.read_text()
    )


def test_streaming_memory_does_not_grow_with_row_count(tmp_path: pathlib.Path) -> None:
    """ADR-024's reason for existing.

    5M MES events held as dicts measured ~4.3 GB. Peak memory must be a
    function of chunk size, not row count, or the full profile is unbuildable
    on a laptop.
    """
    spec = writers.ExtractSpec(
        name="probe",
        columns=["serial_number", "amount_usd"],
        source_system="MES",
        value_column="amount_usd",
        natural_key=["serial_number"],
    )

    def chunks(total: int, size: int):
        made = 0
        while made < total:
            count = min(size, total - made)
            yield [
                {"serial_number": f"SN{made + i:09d}", "amount_usd": D("0.00")}
                for i in range(count)
            ]
            made += count

    writers.write_extract_streaming(spec, chunks(20_000, 5_000), tmp_path / "small", STAMP)
    small = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    writers.write_extract_streaming(spec, chunks(400_000, 5_000), tmp_path / "big", STAMP)
    big = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # 20x the rows must not mean materially more memory. Peak RSS never falls,
    # so this compares the high-water mark before and after.
    growth = (big - small) / max(small, 1)
    assert growth < 0.25, f"peak RSS grew {growth:.0%} for 20x the rows; the writer is buffering"


def test_streaming_writer_rejects_unrounded_money(tmp_path: pathlib.Path) -> None:
    """The guard must survive the streaming rewrite."""
    spec = writers.ExtractSpec(
        name="probe",
        columns=["id", "amount_usd"],
        source_system="ERP",
        value_column="amount_usd",
        natural_key=["id"],
    )
    with pytest.raises(ValueError, match="two decimal places"):
        writers.write_extract_streaming(
            spec, [[{"id": "1", "amount_usd": D("1.005")}]], tmp_path, STAMP
        )


def test_streaming_writer_rejects_a_missing_column(tmp_path: pathlib.Path) -> None:
    spec = writers.ExtractSpec(
        name="probe",
        columns=["id", "name"],
        source_system="ERP",
        value_column=None,
        natural_key=["id"],
    )
    with pytest.raises(ValueError, match="missing columns"):
        writers.write_extract_streaming(spec, [[{"id": "1"}]], tmp_path, STAMP)


def test_the_master_data_is_independent_of_the_generators_that_follow_it(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """The masters must not shift when later generators are added or changed.

    This replaces a test that asserted the whole inventory slice was
    byte-identical with and without the BOM step. That premise is gone:
    ADR-027 made purchase quantities depend on what production will consume,
    and made the goods issues *be* that consumption, so inventory now depends
    on the bill of materials by design. Keeping the old assertion would be
    asserting the coupling had not happened.

    What survives is the part that still matters. The material, vendor and
    standard cost masters are drawn before anything downstream exists, so they
    must be identical whether or not the rest of the run happens. If they were
    not, every figure in the README would move whenever a later generator
    changed how many random draws it made -- the fault the original test was
    written to catch, and still worth catching.
    """
    standalone = inventory.InventoryGenerator(cfg, "demo")
    materials = standalone.materials()
    suppliers = standalone.suppliers()
    standard_costs, _ = standalone.standard_costs(materials)

    built, _ = dataset.build(cfg, "demo")
    extracts = built.as_mapping()

    assert extracts["materials"] == materials
    assert extracts["suppliers"] == suppliers

    # Standard costs are the exception, and deliberately so. A manufactured
    # material's cost is not a negotiated price but the sum of what goes into
    # it, so BTB-35 rolls it up once the bills of materials exist and writes it
    # back over whatever the master drew -- rack 1000008 was carrying 8,461
    # against a BOM worth 1,163,783. Every OTHER material's cost must still be
    # untouched, or something downstream is reaching back into the master.
    manufactured = {header["material_number"] for header in extracts["bom_headers"]}
    assert manufactured, "no manufactured materials, so the exemption proves nothing"

    def windows(rows: list[dict[str, Any]]) -> dict[str, list[tuple[Any, Any, Any]]]:
        by_material: dict[str, list[tuple[Any, Any, Any]]] = {}
        for row in rows:
            by_material.setdefault(row["material_number"], []).append(
                (row["valid_from"], row["valid_to"], row["standard_cost_usd"])
            )
        return by_material

    drawn = windows(standard_costs)
    final = windows(extracts["standard_costs"])

    for material, drawn_windows in drawn.items():
        if material in manufactured:
            continue
        assert final.get(material) == drawn_windows, (
            f"{material} is bought, not built, but its standard cost changed "
            "after the master was drawn"
        )

    # And the roll-up must have actually happened, or the exemption above is
    # excusing nothing and would hide a real drift.
    for material in sorted(manufactured):
        assert len(final[material]) == 1, (
            f"{material} should carry one rolled-up window, not {len(final[material])}"
        )
        assert final[material] != drawn.get(material), (
            f"{material} is manufactured but its cost was not rolled up"
        )


def test_the_masters_are_memoised_so_asking_twice_is_the_same_answer(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """Calling `materials()` twice must not draw twice.

    `dataset.build` needs the material master before the bill of materials and
    again inside `generate()`. Without memoisation the second call drew from
    the shared generator and returned a *different* master: the BOM referenced
    materials the cost windows had never heard of, and generation died on a
    KeyError for material 1000007.
    """
    generator = inventory.InventoryGenerator(cfg, "demo")
    assert generator.materials() == generator.materials()
    assert generator.suppliers() == generator.suppliers()

    # And the second call must not consume randomness, or everything generated
    # afterwards shifts depending on how many times somebody happened to ask.
    once_gen = inventory.InventoryGenerator(cfg, "demo")
    once = once_gen.purchase_orders(once_gen.materials(), once_gen.suppliers())

    twice_gen = inventory.InventoryGenerator(cfg, "demo")
    twice_gen.materials()
    twice_gen.suppliers()
    twice = twice_gen.purchase_orders(twice_gen.materials(), twice_gen.suppliers())

    assert once == twice, "asking for the masters twice changed what came after"


def test_stamped_columns_are_real_columns_of_their_extract() -> None:
    """A declared stamped column must exist, or the declaration hides nothing.

    `STAMPED_COLUMNS` is what lets the order-independence test above ignore a
    legitimate difference. A typo in it would silently widen that exemption to
    a column nobody is actually stamping, which is exactly the kind of quiet
    loosening that makes a test agree with the code instead of with reality.
    """
    for name, columns in dataset.STAMPED_COLUMNS.items():
        assert name in dataset.EXTRACT_SPECS, f"{name} is not a declared extract"
        declared = set(dataset.EXTRACT_SPECS[name].columns)
        unknown = sorted(set(columns) - declared)
        assert not unknown, f"{name} declares stamped column(s) it does not have: {unknown}"


def test_a_stamped_column_is_populated_by_the_end_of_a_full_build(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """The other direction: the stamp must actually happen.

    Emitting None and declaring it stamped would otherwise be enough to satisfy
    everything above while the value was never written -- the extract would load
    cleanly with a null column and the only symptom would be an empty join.
    """
    data, _ = dataset.build(cfg, "demo")
    # materialise(), because `goods_movements` is a deferred extract now
    # (ADR-028) and is absent from as_mapping() by design.
    extracts = data.materialise()
    for name, columns in dataset.STAMPED_COLUMNS.items():
        rows = extracts[name]
        for column in columns:
            populated = sum(1 for row in rows if row[column] is not None)
            assert populated > 0, (
                f"{name}.{column} is declared stamped but no row has a value after a full build"
            )


def test_every_rack_configuration_has_trays_that_can_be_built(
    cfg: genconfig.GeneratorConfig, built: tuple[dataset.Dataset, list]
) -> None:
    """A rack with no sub-assemblies is unbuildable, and was silently possible.

    Racks used to draw their trays from the whole CHASSIS group while only a
    prepared pool had bills of materials of their own. A rack that happened to
    draw none of the pool had every tray skipped as a dead end and came out
    empty. Nothing downstream asked whether a rack configuration had content,
    so it would have produced orders that consumed nothing.

    Latent from the start. It first fired when ADR-027 reordered generation and
    the draws fell differently -- which is the whole argument for not trusting
    the generator: this is a property of the data, not of the code path that
    happened to run.
    """
    data, _ = built
    assert data.rack_configurations

    empty = sorted(rack for rack, trays in data.rack_configurations.items() if not trays)
    assert not empty, f"rack configuration(s) with no trays: {empty}"

    # And every named tray must itself have a bill of materials, or it is a
    # leaf posing as a sub-assembly.
    extracts = data.as_mapping()
    parents_with_boms = {header["material_number"] for header in extracts["bom_headers"]}
    for rack, trays in data.rack_configurations.items():
        for tray in trays:
            assert tray in parents_with_boms, (
                f"rack {rack} names tray {tray}, which has no bill of materials"
            )


def test_every_production_order_consumes_something(
    built: tuple[dataset.Dataset, list],
) -> None:
    """The downstream consequence the test above prevents.

    An empty rack configuration produces orders that consume no components, and
    those orders would then report a zero cost of production while MES happily
    recorded units passing test. Worth asserting separately: the cause is in the
    bill of materials, but this is the symptom a reviewer would actually see.
    """
    data, _ = built
    extracts = data.as_mapping()
    consuming = {line["order_number"] for line in extracts["component_consumption"]}
    orders = {order["order_number"] for order in extracts["production_orders"]}
    assert orders
    assert orders == consuming, (
        f"{len(orders - consuming)} order(s) consume nothing, so their cost of "
        "production is zero while MES records units built"
    )
