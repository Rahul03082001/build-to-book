"""The inventory generator's invariants.

With defects off, the generated data must be internally consistent, or every
downstream control is measuring the generator's bugs instead of the defects it
plants. These are the assertions that make "clean data is actually clean" a
tested property rather than an assumption:

* deterministic for a given seed, byte for byte
* every goods receipt references a real PO line
* on-hand never goes negative
* the subledger ties to the GL, per period, to the cent
* every accounting document balances
* each extract's control totals match the extract

The subledger-to-GL tie is the important one. CTRL-003 compares those two
figures, so if they disagreed here the control could never pass and we would
have no way to tell a real finding from a generator fault.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import decimal
import filecmp
import itertools
import json
import pathlib
from typing import Any

import pytest

from generator import config as genconfig
from generator import inventory, writers

D = decimal.Decimal


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


@pytest.fixture(scope="module")
def data(cfg: genconfig.GeneratorConfig) -> inventory.InventoryData:
    """The inventory extracts out of a **full** build, once per module.

    The inventory slice can no longer be generated on its own. Its purchase
    quantities are sized to what production will consume, and its goods issues
    *are* that consumption (ADR-027), so generating it in isolation yields
    receipts with no issues at all -- closing inventory equal to 100% of
    receipts, which is exactly what three tests in this module reported when
    the fixture still did that.

    The coupling is deliberate: inventory and production describe one physical
    flow rather than two unrelated ones. A fixture that pretends otherwise is
    testing a configuration the project no longer produces.
    """
    from generator import dataset

    built, _ = dataset.build(cfg, "demo")
    extracts = built.as_mapping()
    return inventory.InventoryData(
        materials=extracts["materials"],
        suppliers=extracts["suppliers"],
        standard_costs=extracts["standard_costs"],
        purchase_order_headers=extracts["purchase_order_headers"],
        purchase_order_lines=extracts["purchase_order_lines"],
        goods_movements=extracts["goods_movements"],
        inventory_revaluations=extracts["inventory_revaluations"],
        gl_postings=extracts["gl_postings"],
        cost_windows=built.cost_windows,
    )


@pytest.fixture(scope="module")
def written(
    cfg: genconfig.GeneratorConfig,
    data: inventory.InventoryData,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, writers.WrittenExtract]:
    out = tmp_path_factory.mktemp("extracts")
    stamp = dt.datetime(2026, 11, 2, 12, 0, 0)
    return {
        name: writers.write_extract(inventory.EXTRACT_SPECS[name], rows, out, stamp)
        for name, rows in data.as_mapping().items()
    }


def _period(value: dt.date) -> str:
    return value.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# BTB-10 / BTB-11 / BTB-12: determinism
# ---------------------------------------------------------------------------
def test_generator_is_deterministic_byte_for_byte(
    cfg: genconfig.GeneratorConfig, tmp_path: pathlib.Path
) -> None:
    """Two runs at the same seed produce identical CSVs.

    Byte equality, not row-count equality: the defect tests assert on exact
    injected records, so a reviewer re-running the project must get the same
    numbers the README quotes.
    """
    stamp = dt.datetime(2026, 11, 2, 12, 0, 0)
    directories = []
    for run in ("first", "second"):
        out = tmp_path / run
        generated = inventory.InventoryGenerator(cfg, "demo").generate()
        for name, rows in generated.as_mapping().items():
            writers.write_extract(inventory.EXTRACT_SPECS[name], rows, out, stamp)
        directories.append(out)

    names = sorted(p.name for p in directories[0].glob("*.csv"))
    assert names, "no extracts written"
    _match, mismatch, errors = filecmp.cmpfiles(
        directories[0], directories[1], names, shallow=False
    )
    assert not mismatch and not errors, f"non-deterministic: {mismatch or errors}"


def test_a_different_seed_produces_different_data(cfg: genconfig.GeneratorConfig) -> None:
    """Guards against the seed being ignored, which would make determinism vacuous."""
    import dataclasses

    other = dataclasses.replace(cfg, seed=cfg.seed + 1)
    first = inventory.InventoryGenerator(cfg, "demo").generate()
    second = inventory.InventoryGenerator(other, "demo").generate()
    assert first.materials != second.materials


# ---------------------------------------------------------------------------
# BTB-10: master data
# ---------------------------------------------------------------------------
def test_volumes_match_the_demo_profile(
    cfg: genconfig.GeneratorConfig, data: inventory.InventoryData
) -> None:
    profile = cfg.profile("demo")
    assert len(data.materials) == profile.materials
    assert len(data.suppliers) == profile.suppliers
    assert len(data.purchase_order_lines) == profile.purchase_order_lines


def test_master_data_keys_are_unique(data: inventory.InventoryData) -> None:
    for rows, key in [
        (data.materials, "material_number"),
        (data.suppliers, "supplier_number"),
        (data.purchase_order_headers, "po_number"),
    ]:
        values = [row[key] for row in rows]
        assert len(values) == len(set(values)), f"duplicate {key}"


def test_every_material_group_is_declared_in_config(
    cfg: genconfig.GeneratorConfig, data: inventory.InventoryData
) -> None:
    declared = set(cfg.domain["material_groups"])
    assert {m["material_group"] for m in data.materials} <= declared


def test_standard_costs_are_effective_dated_without_gaps_or_overlaps(
    data: inventory.InventoryData,
) -> None:
    """An as-of join depends on exactly one cost being in force on any date.

    A gap makes valuation fail; an overlap silently duplicates fact rows when
    joined, which is the classic SCD Type 2 bug (ADR-007).
    """
    by_material: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in data.standard_costs:
        by_material[row["material_number"]].append(row)

    for material_number, rows in by_material.items():
        windows = sorted((r["valid_from"], r["valid_to"]) for r in rows)
        for (_, prev_to), (next_from, _) in itertools.pairwise(windows):
            assert next_from == prev_to + dt.timedelta(days=1), (
                f"{material_number} has a gap or overlap at {prev_to} -> {next_from}"
            )
        assert windows[-1][1] == dt.date(9999, 12, 31), (
            f"{material_number}'s last cost window is not open-ended"
        )


def test_some_materials_have_a_mid_period_cost_change(data: inventory.InventoryData) -> None:
    """Otherwise an as-of join bug would be invisible, and Phase 3's snapshot trivial."""
    counts = collections.Counter(r["material_number"] for r in data.standard_costs)
    assert sum(1 for n in counts.values() if n > 1) >= 1


def test_every_cost_has_an_approval_record(data: inventory.InventoryData) -> None:
    """CTRL-007 compares changes to approvals, so clean data must have them all."""
    for row in data.standard_costs:
        assert row["approved_by"], f"{row['material_number']} cost change has no approver"
        assert row["approved_on"] <= row["valid_from"], "approved after it took effect"


# ---------------------------------------------------------------------------
# BTB-11: purchasing and movements
# ---------------------------------------------------------------------------
def test_every_po_line_belongs_to_a_real_header(data: inventory.InventoryData) -> None:
    headers = {h["po_number"] for h in data.purchase_order_headers}
    orphans = [line for line in data.purchase_order_lines if line["po_number"] not in headers]
    assert not orphans, f"{len(orphans)} PO lines have no header"


def test_po_line_net_value_equals_quantity_times_price(data: inventory.InventoryData) -> None:
    for line in data.purchase_order_lines:
        expected = writers.money(line["unit_price_usd"] * line["quantity_ordered"])
        assert line["net_value_usd"] == expected, f"{line['po_number']}/{line['po_item']}"


def test_every_goods_receipt_references_a_real_po_line(data: inventory.InventoryData) -> None:
    """Without this the three-way match has nothing to match."""
    po_keys = {(line["po_number"], line["po_item"]) for line in data.purchase_order_lines}
    receipts = [
        m for m in data.goods_movements if m["movement_type"] == inventory.MVT_GOODS_RECEIPT
    ]
    assert receipts, "no receipts generated"
    orphans = [m for m in receipts if (m["po_number"], m["po_item"]) not in po_keys]
    assert not orphans, f"{len(orphans)} receipts reference a PO line that does not exist"


def test_goods_issues_carry_no_purchase_order_reference(data: inventory.InventoryData) -> None:
    issues = [m for m in data.goods_movements if m["movement_type"] == inventory.MVT_GOODS_ISSUE]
    assert issues, "no issues generated"
    assert all(m["po_number"] is None for m in issues)


def test_movement_type_determines_the_sign_of_the_quantity(data: inventory.InventoryData) -> None:
    """Getting this wrong inverts the balance sheet, so it is asserted directly."""
    for movement in data.goods_movements:
        sign = inventory.MOVEMENT_SIGN[movement["movement_type"]]
        assert movement["quantity"] != 0
        assert (movement["quantity"] > 0) == (sign > 0), movement["material_document"]
        assert (movement["amount_usd"] > 0) == (sign > 0), movement["material_document"]


def test_on_hand_never_goes_negative(data: inventory.InventoryData) -> None:
    """Negative on-hand is physically impossible, so clean data must not show it.

    Phase 2's `negative_on_hand` defect creates it deliberately by posting an
    issue before the receipt that covers it; CTRL-010 then has something real
    to detect.
    """
    by_material: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for movement in data.goods_movements:
        by_material[movement["material_number"]].append(movement)

    for material_number, movements in by_material.items():
        ordered = sorted(
            movements,
            key=lambda m: (m["posting_date"], m["material_document"], m["material_document_item"]),
        )
        balance = 0
        for movement in ordered:
            balance += movement["quantity"]
            assert balance >= 0, (
                f"{material_number} went negative ({balance}) on "
                f"{movement['posting_date']} via {movement['material_document']}"
            )


def test_material_documents_are_unique_within_a_fiscal_year(
    data: inventory.InventoryData,
) -> None:
    """The natural key is (document, year, item), not the document alone.

    Material document numbers legitimately repeat across fiscal years, so a
    loader keyed on the document would collide silently (ADR-020).
    """
    keys = [
        (m["material_document"], m["fiscal_year"], m["material_document_item"])
        for m in data.goods_movements
    ]
    duplicates = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not duplicates, f"duplicate material document keys: {duplicates[:5]}"


def test_at_least_one_document_carries_multiple_items(data: inventory.InventoryData) -> None:
    """Otherwise document and line grain are identical and declaring a grain proves nothing."""
    assert max(m["material_document_item"] for m in data.goods_movements) > 1


def test_postings_fall_inside_the_profile_window(
    cfg: genconfig.GeneratorConfig, data: inventory.InventoryData
) -> None:
    generator = inventory.InventoryGenerator(cfg, "demo")
    for movement in data.goods_movements:
        assert generator.start <= movement["posting_date"] <= generator.end
        # Paperwork is raised on or before the posting hits the books.
        assert movement["document_date"] <= movement["posting_date"]


# ---------------------------------------------------------------------------
# BTB-12: the GL tie
# ---------------------------------------------------------------------------
def test_every_accounting_document_balances_to_zero(data: inventory.InventoryData) -> None:
    balances: dict[tuple[str, int], decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for posting in data.gl_postings:
        balances[(posting["accounting_document"], posting["fiscal_year"])] += posting[
            "signed_amount_usd"
        ]
    unbalanced = {k: v for k, v in balances.items() if v != 0}
    assert not unbalanced, f"unbalanced documents: {list(unbalanced.items())[:5]}"


def test_subledger_ties_to_the_gl_per_period(data: inventory.InventoryData) -> None:
    """CTRL-003's premise: with defects off the difference is exactly zero.

    The subledger is goods movements **plus standard cost revaluations**. Both
    are inventory transactions: a movement changes quantity and value, a
    revaluation changes value only. Omitting revaluations was what let the
    inventory account drift away from quantity x standard cost.

    Compared to the cent, not within a tolerance. A tolerance here would hide
    the rounding faults the money guard in writers.py exists to prevent.
    """
    subledger: dict[str, decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for movement in data.goods_movements:
        subledger[_period(movement["posting_date"])] += movement["amount_usd"]
    for revaluation in data.inventory_revaluations:
        subledger[_period(revaluation["posting_date"])] += revaluation["amount_usd"]

    ledger: dict[str, decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for posting in data.gl_postings:
        if posting["gl_account"] == inventory.ACCOUNT_INVENTORY:
            ledger[_period(posting["posting_date"])] += posting["signed_amount_usd"]

    assert set(subledger) == set(ledger), "periods differ between subledger and GL"
    for period in sorted(subledger):
        assert subledger[period] == ledger[period], (
            f"{period}: subledger {subledger[period]} != GL {ledger[period]} "
            f"(difference {subledger[period] - ledger[period]})"
        )


def test_closing_inventory_is_materially_positive(data: inventory.InventoryData) -> None:
    """Inventory value and aging are this slice's headline KPIs.

    A business that issues everything it receives has nothing to value, so a
    near-zero closing balance would make the dashboard meaningless even though
    every other invariant held.
    """
    closing = sum((m["amount_usd"] for m in data.goods_movements), D(0)) + sum(
        (r["amount_usd"] for r in data.inventory_revaluations), D(0)
    )
    assert closing > D("500000"), f"closing inventory is only {closing}"


def test_every_gl_posting_references_a_real_source_document(
    data: inventory.InventoryData,
) -> None:
    """Every posting traces to a material document or a revaluation document."""
    documents = {(m["material_document"], m["fiscal_year"]) for m in data.goods_movements}
    documents |= {
        (r["revaluation_document"], r["fiscal_year"]) for r in data.inventory_revaluations
    }
    orphans = [
        p for p in data.gl_postings if (p["reference_document"], p["fiscal_year"]) not in documents
    ]
    assert not orphans, f"{len(orphans)} GL postings reference no source document"


def test_inventory_lines_carry_a_material_and_offsets_do_not(
    data: inventory.InventoryData,
) -> None:
    for posting in data.gl_postings:
        if posting["gl_account"] == inventory.ACCOUNT_INVENTORY:
            assert posting["material_number"], "inventory line has no material"
        else:
            assert posting["material_number"] is None, "offset line should carry no material"


# ---------------------------------------------------------------------------
# BTB-13: control totals
# ---------------------------------------------------------------------------
def test_every_extract_has_a_control_totals_sidecar(
    written: dict[str, writers.WrittenExtract],
) -> None:
    assert set(written) == set(inventory.EXTRACT_SPECS)
    for name, result in written.items():
        assert result.control_totals_path.exists(), f"{name} has no sidecar"


def test_control_totals_match_the_extract_they_accompany(
    written: dict[str, writers.WrittenExtract],
) -> None:
    """Row count, value total and hash are all recomputed from the file."""
    for name, result in written.items():
        payload = json.loads(result.control_totals_path.read_text())
        with result.path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        assert payload["row_count"] == len(rows) == result.row_count, name

        import hashlib

        assert payload["sha256"] == hashlib.sha256(result.path.read_bytes()).hexdigest(), name

        value_column = payload["value_column"]
        if value_column is None:
            assert payload["value_total"] is None, name
            continue
        # Re-add from the file's own text, which is what a loader does.
        total = sum((D(row[value_column]) for row in rows), D(0))
        assert str(total) == payload["value_total"], (
            f"{name}: CSV re-adds to {total} but the sidecar says {payload['value_total']}"
        )


def test_sidecar_declares_the_natural_key_the_loader_will_use(
    written: dict[str, writers.WrittenExtract],
) -> None:
    for name, result in written.items():
        payload = json.loads(result.control_totals_path.read_text())
        assert payload["natural_key"] == list(inventory.EXTRACT_SPECS[name].natural_key)
        assert payload["natural_key"], f"{name} declares no natural key"


def test_sidecar_labels_the_data_synthetic(
    written: dict[str, writers.WrittenExtract],
) -> None:
    """ADR-019 requires the label on every published surface, exports included."""
    for result in written.values():
        payload = json.loads(result.control_totals_path.read_text())
        assert "SYNTHETIC" in payload["notice"].upper()


def test_a_missing_sidecar_is_a_hard_failure(tmp_path: pathlib.Path) -> None:
    """Deleting a sidecar must not quietly skip the completeness check (ADR-022)."""
    with pytest.raises(FileNotFoundError, match="control totals missing"):
        writers.read_control_totals(tmp_path / "absent.control_totals.json")


def test_natural_keys_are_unique_in_every_extract(data: inventory.InventoryData) -> None:
    """ADR-020's anti-join assumes the declared key identifies a row."""
    for name, rows in data.as_mapping().items():
        key = inventory.EXTRACT_SPECS[name].natural_key
        seen = [tuple(row[column] for column in key) for row in rows]
        duplicates = [k for k, n in collections.Counter(seen).items() if n > 1]
        assert not duplicates, f"{name} has duplicate natural keys: {duplicates[:3]}"


def test_movements_are_valued_at_the_posting_date_cost(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """Valuation uses the posting date, not the document date.

    docs/metric_definitions.md settles posting date as the basis for every
    metric, because finance reports on when a transaction hit the books. The
    document date is when the paperwork was raised, and the two straddle a
    period boundary often enough to matter at month end.

    Nothing else in the suite catches this: the GL postings derive their
    amounts from the movement, so switching the basis moves both together and
    the subledger still ties to the GL. The error would be invisible and
    wrong -- a whole period valued at the wrong standard cost.

    The test also asserts that the generated data *can* tell the two apart. If
    no movement straddled a cost change, this would pass against either basis
    and prove nothing.
    """
    generator = inventory.InventoryGenerator(cfg, "demo")
    materials = generator.materials()
    suppliers = generator.suppliers()
    _costs, lookup = generator.standard_costs(materials)
    _headers, lines = generator.purchase_orders(materials, suppliers)
    movements = generator.goods_movements(lines, lookup)

    discriminating = 0
    for movement in movements:
        material_number = movement["material_number"]
        posting_cost = generator.cost_on(lookup, material_number, movement["posting_date"])
        document_cost = generator.cost_on(lookup, material_number, movement["document_date"])
        if posting_cost != document_cost:
            discriminating += 1

        expected = writers.money(posting_cost * movement["quantity"])
        assert movement["amount_usd"] == expected, (
            f"{movement['material_document']}/{movement['material_document_item']} "
            f"valued at {movement['amount_usd']}, expected {expected} at the "
            f"posting-date cost of {posting_cost}"
        )

    assert discriminating > 0, (
        "no movement straddles a standard cost change, so this test cannot "
        "distinguish posting-date from document-date valuation -- raise the "
        "cost-change rate in standard_costs() or widen the document-date lag"
    )


# ---------------------------------------------------------------------------
# Standard cost revaluation
# ---------------------------------------------------------------------------
def test_closing_value_equals_quantity_times_current_standard_cost(
    cfg: genconfig.GeneratorConfig, data: inventory.InventoryData
) -> None:
    """The invariant revaluation exists to restore, asserted per material.

    Standard costing values each movement at the standard in force on its
    posting date, so stock bought at the old cost and issued at the new one
    leaves a value residue after the quantity has netted out. Revaluing on-hand
    stock when the cost changes removes it, and this is the test that proves it:
    closing value must be exactly on-hand quantity times the current standard
    cost, for every material.

    Before revaluation existed, 20 of 300 materials on the full profile closed
    at a *negative* value while every physical quantity was correct.

    Deliberately run against ``generate()``'s own output rather than a pipeline
    the test assembles itself. An earlier version built its own, which meant
    removing the revaluation step from ``generate()`` left this -- the
    strongest guard -- still passing.
    """
    generator = inventory.InventoryGenerator(cfg, "demo")

    quantity: dict[str, int] = collections.defaultdict(int)
    value: dict[str, decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for movement in data.goods_movements:
        quantity[movement["material_number"]] += movement["quantity"]
        value[movement["material_number"]] += movement["amount_usd"]
    for revaluation in data.inventory_revaluations:
        value[revaluation["material_number"]] += revaluation["amount_usd"]

    assert data.cost_windows, "generate() did not expose the cost windows"
    for material_number, closing_quantity in quantity.items():
        current_cost = generator.cost_on(data.cost_windows, material_number, generator.end)
        expected = writers.money(current_cost * closing_quantity)
        assert value[material_number] == expected, (
            f"{material_number}: closing value {value[material_number]} but "
            f"{closing_quantity} x {current_cost} = {expected}"
        )


def test_no_material_closes_at_a_negative_value(data: inventory.InventoryData) -> None:
    value: dict[str, decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for movement in data.goods_movements:
        value[movement["material_number"]] += movement["amount_usd"]
    for revaluation in data.inventory_revaluations:
        value[revaluation["material_number"]] += revaluation["amount_usd"]
    negative = {k: v for k, v in value.items() if v < 0}
    assert not negative, f"materials closing at a negative value: {list(negative.items())[:5]}"


def test_revaluations_change_value_without_moving_quantity(
    data: inventory.InventoryData,
) -> None:
    """Nothing physically moved, so a revaluation carries no quantity movement."""
    assert data.inventory_revaluations, "no revaluations generated"
    for revaluation in data.inventory_revaluations:
        assert revaluation["quantity_on_hand"] > 0, "revaluing nothing"
        assert revaluation["amount_usd"] != 0
        expected = writers.money(
            (revaluation["new_standard_cost_usd"] - revaluation["old_standard_cost_usd"])
            * revaluation["quantity_on_hand"]
        )
        assert revaluation["amount_usd"] == expected


def test_every_revaluation_names_the_approver_of_the_cost_change(
    data: inventory.InventoryData,
) -> None:
    """CTRL-007 joins cost changes to approvals; the revaluation carries it through."""
    for revaluation in data.inventory_revaluations:
        assert revaluation["approved_by"], (
            f"revaluation {revaluation['revaluation_document']} has no approver"
        )


def test_inventory_retains_a_realistic_share_of_receipts(
    data: inventory.InventoryData,
) -> None:
    """A manufacturer holds weeks of stock, not zero and not everything.

    The full profile previously closed at exactly zero because issue volume was
    derived from a target posting count rather than from receipts.
    """
    received = sum(
        (
            m["amount_usd"]
            for m in data.goods_movements
            if m["movement_type"] == inventory.MVT_GOODS_RECEIPT
        ),
        D(0),
    )
    closing = sum((m["amount_usd"] for m in data.goods_movements), D(0)) + sum(
        (r["amount_usd"] for r in data.inventory_revaluations), D(0)
    )
    share = closing / received
    assert D("0.05") < share < D("0.60"), f"closing inventory is {share:.1%} of receipts"
