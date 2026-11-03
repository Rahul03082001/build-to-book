"""Supplier invoices and production orders (BTB-31, BTB-32).

Two properties matter most here, and both are about not fooling ourselves:

* An invoice must bill what was **received**, not what was ordered. Billing the
  ordered quantity would make the three-way match pass even on a short
  delivery, so CTRL-004's clean baseline would be meaningless.
* Component consumption must follow the **exploded BOM**. If it did not, the
  Phase 3 variance metrics would be measuring the generator.

There is also a guard on a duplication introduced deliberately:
`production.explode_bom` mirrors `int_bom_exploded` in Python because the
generator cannot query a dbt model that does not exist yet. A test asserts the
two agree, so the mirror cannot drift from the model ADR-025 made
authoritative.
"""

from __future__ import annotations

import collections
import decimal
import pathlib
from typing import Any

import conftest
import psycopg
import pytest

from generator import config as genconfig
from generator import dataset, production

D = decimal.Decimal


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


@pytest.fixture(scope="module")
def built(cfg: genconfig.GeneratorConfig) -> dataset.Dataset:
    data, _ = dataset.build(cfg, "demo")
    return data


# ---------------------------------------------------------------------------
# BTB-31: supplier invoices
# ---------------------------------------------------------------------------
def test_invoice_header_gross_equals_the_sum_of_its_lines(
    built: dataset.Dataset,
) -> None:
    lines_by_invoice: dict[tuple[str, int], decimal.Decimal] = collections.defaultdict(lambda: D(0))
    for line in built.as_mapping()["supplier_invoice_lines"]:
        key = (line["invoice_number"], line["fiscal_year"])
        lines_by_invoice[key] += line["net_amount_usd"]

    for header in built.as_mapping()["supplier_invoice_headers"]:
        key = (header["invoice_number"], header["fiscal_year"])
        assert lines_by_invoice[key] == header["gross_amount_usd"], key


def test_every_invoice_line_references_a_real_po_line(built: dataset.Dataset) -> None:
    """CTRL-009's clean baseline.

    Added after testing CTRL-004 showed that an orphan invoice line is
    invisible to the three-way match -- the match is driven from PO lines, so
    the join excludes it rather than flagging it.
    """
    po_keys = {
        (line["po_number"], line["po_item"]) for line in built.as_mapping()["purchase_order_lines"]
    }
    orphans = [
        line
        for line in built.as_mapping()["supplier_invoice_lines"]
        if (line["po_number"], line["po_item"]) not in po_keys
    ]
    assert not orphans, f"{len(orphans)} invoice lines reference no PO line"


def test_invoices_bill_the_quantity_received_not_ordered(
    built: dataset.Dataset,
) -> None:
    """The property CTRL-004's baseline depends on.

    Billing the ordered quantity would make the three-way match pass on a
    short delivery, so the control could never detect one.
    """
    received: dict[tuple[str, int], int] = collections.defaultdict(int)
    for movement in built.as_mapping()["goods_movements"]:
        if movement["movement_type"] == "101":
            received[(movement["po_number"], movement["po_item"])] += movement["quantity"]

    ordered = {
        (line["po_number"], line["po_item"]): line["quantity_ordered"]
        for line in built.as_mapping()["purchase_order_lines"]
    }

    differs_from_ordered = 0
    for line in built.as_mapping()["supplier_invoice_lines"]:
        key = (line["po_number"], line["po_item"])
        assert line["quantity_invoiced"] == received[key], (
            f"{key}: invoiced {line['quantity_invoiced']} but received {received[key]}"
        )
        if line["quantity_invoiced"] != ordered[key]:
            differs_from_ordered += 1

    # If every receipt happened to equal its order, the assertion above would
    # pass even on a generator that billed the ordered quantity.
    assert differs_from_ordered > 0, (
        "no invoice differs from its ordered quantity, so this test cannot "
        "distinguish billing received from billing ordered"
    )


def test_invoice_prices_match_the_purchase_order(built: dataset.Dataset) -> None:
    """Clean data has no price variance, so CTRL-004 starts at zero exceptions."""
    po_price = {
        (line["po_number"], line["po_item"]): line["unit_price_usd"]
        for line in built.as_mapping()["purchase_order_lines"]
    }
    for line in built.as_mapping()["supplier_invoice_lines"]:
        key = (line["po_number"], line["po_item"])
        assert line["unit_price_usd"] == po_price[key], key


def test_some_receipts_are_not_invoiced(built: dataset.Dataset) -> None:
    """Goods received not invoiced is a real balance at period end.

    If everything received were invoiced, the GR/IR clearing account would
    always be zero and the three-way match's RECEIVED_NOT_INVOICED status would
    be unreachable.
    """
    invoiced = {
        (line["po_number"], line["po_item"])
        for line in built.as_mapping()["supplier_invoice_lines"]
    }
    receipted = {
        (movement["po_number"], movement["po_item"])
        for movement in built.as_mapping()["goods_movements"]
        if movement["movement_type"] == "101"
    }
    assert receipted - invoiced, "every receipt was invoiced, so GR/IR is always zero"


def test_payment_terms_come_from_the_supplier_master(built: dataset.Dataset) -> None:
    """They were read from the PO header, which does not carry them.

    Every invoice silently got the "NET30" default, so the column looked
    populated while meaning nothing.
    """
    supplier_terms = {
        supplier["supplier_number"]: supplier["payment_terms"]
        for supplier in built.as_mapping()["suppliers"]
    }
    for header in built.as_mapping()["supplier_invoice_headers"]:
        assert header["payment_terms"] == supplier_terms[header["supplier_number"]]

    distinct = {h["payment_terms"] for h in built.as_mapping()["supplier_invoice_headers"]}
    assert len(distinct) > 1, "every invoice has the same terms; the default is leaking"


# ---------------------------------------------------------------------------
# BTB-32: production orders
# ---------------------------------------------------------------------------
def test_every_production_order_builds_a_rack_configuration(
    built: dataset.Dataset,
) -> None:
    for order in built.as_mapping()["production_orders"]:
        assert order["material_number"] in built.rack_configurations, order


def test_consumption_follows_the_exploded_bom(built: dataset.Dataset) -> None:
    """Quantity consumed must be the exploded requirement times the order quantity.

    If it did not follow the BOM, the Phase 3 variance metrics would be
    measuring the generator rather than the business.
    """
    requirements = {
        rack: production.explode_bom(
            built.as_mapping()["bom_headers"], built.as_mapping()["bom_items"], rack
        )
        for rack in built.rack_configurations
    }
    orders = {o["order_number"]: o for o in built.as_mapping()["production_orders"]}

    for row in built.as_mapping()["component_consumption"]:
        order = orders[row["order_number"]]
        per_unit = requirements[order["material_number"]][row["component_material_number"]]
        assert row["quantity_consumed"] == per_unit * order["target_quantity"], row


def test_every_order_consumes_something(built: dataset.Dataset) -> None:
    consuming = {r["order_number"] for r in built.as_mapping()["component_consumption"]}
    orders = {o["order_number"] for o in built.as_mapping()["production_orders"]}
    assert orders == consuming, "an order that consumes nothing cannot build anything"


def test_orders_finish_inside_the_reporting_window(
    cfg: genconfig.GeneratorConfig, built: dataset.Dataset
) -> None:
    from generator import inventory

    generator = inventory.InventoryGenerator(cfg, "demo")
    for order in built.as_mapping()["production_orders"]:
        assert generator.start <= order["start_date"] <= generator.end
        assert order["finish_date"] <= generator.end
        assert order["order_date"] <= order["start_date"]


def test_confirmations_are_absent_until_mes_exists() -> None:
    """Deliberately not generated yet (BTB-33).

    A confirmation records how many units an order completed, and CTRL-005
    compares that against the units MES says passed final test. Generating the
    two independently would leave a gap made of noise, and a control whose
    clean-run baseline is noise cannot detect a real finding.
    """
    assert "production_confirmations" not in dataset.LOAD_ORDER
    assert "production_confirmations" in production.EXTRACT_SPECS, (
        "the spec should be ready for BTB-33"
    )
    assert hasattr(production.ProductionGenerator, "confirm")


# ---------------------------------------------------------------------------
# The Python mirror of int_bom_exploded
# ---------------------------------------------------------------------------
def test_python_and_dbt_bom_explosions_agree(built: dataset.Dataset) -> None:
    """Guards a duplication introduced on purpose.

    `production.explode_bom` mirrors `int_bom_exploded` because the generator
    cannot query a dbt model that does not exist yet. ADR-025 makes the
    warehouse's explosion authoritative, so the mirror must not drift from it.

    Needs a built warehouse; skips rather than failing when there is not one.
    """
    try:
        from ingestion import db

        connection = db.connect()
    except Exception as error:
        pytest.skip(f"warehouse unavailable: {error}")

    with connection:
        try:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT top_material_number, component_material_number, "
                    "       quantity_required "
                    "FROM intermediate.int_component_requirements"
                )
                rows = cursor.fetchall()
        except psycopg.Error as error:
            pytest.skip(f"int_component_requirements unavailable: {error}")

    if not rows:
        pytest.skip("int_component_requirements is empty; run `make build`")

    from_dbt = {(top, component): quantity for top, component, quantity in rows}
    from_python: dict[tuple[str, str], int] = {}
    for rack in built.rack_configurations:
        for component, quantity in production.explode_bom(
            built.as_mapping()["bom_headers"], built.as_mapping()["bom_items"], rack
        ).items():
            from_python[(rack, component)] = quantity

    # dbt explodes every parent, including trays; the Python mirror is only
    # asked about racks. Compare the racks.
    racks = set(built.rack_configurations)
    dbt_racks = {k: v for k, v in from_dbt.items() if k[0] in racks}

    assert dbt_racks == from_python, (
        "the Python mirror and int_component_requirements disagree: "
        f"only in dbt {set(dbt_racks) - set(from_python)}, "
        f"only in Python {set(from_python) - set(dbt_racks)}"
    )


def test_explode_bom_refuses_to_recurse_forever() -> None:
    """A cyclic BOM must terminate rather than hang the generator."""
    headers = [
        {"bom_number": "B1", "material_number": "A"},
        {"bom_number": "B2", "material_number": "B"},
    ]
    items = [
        {"bom_number": "B1", "component_material_number": "B", "quantity_per": 2},
        # B contains A: a cycle.
        {"bom_number": "B2", "component_material_number": "A", "quantity_per": 3},
    ]
    result = production.explode_bom(headers, items, "A")
    assert isinstance(result, dict), "the explosion did not terminate"


def test_dataset_exposes_the_order_plan(built: dataset.Dataset) -> None:
    """MES generates serials per production order, so it needs the plan."""
    assert built.order_plan
    assert set(built.order_plan) == {
        o["order_number"] for o in built.as_mapping()["production_orders"]
    }
    for plan in built.order_plan.values():
        assert plan["target_quantity"] > 0
        assert plan["start_date"] <= plan["finish_date"]


def test_conftest_load_order_matches_the_generator() -> None:
    assert conftest.LOAD_ORDER == dataset.LOAD_ORDER


def test_raw_ddl_and_sources_cover_the_new_extracts() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    ddl = (root / "warehouse" / "sql" / "raw.sql").read_text()
    sources = (root / "warehouse" / "dbt" / "models" / "staging" / "_sources.yml").read_text()
    for name in (
        "supplier_invoice_headers",
        "supplier_invoice_lines",
        "production_orders",
        "component_consumption",
    ):
        assert f"raw.{name} (" in ddl, f"raw.sql has no table for {name}"
        assert f"- name: {name}" in sources, f"_sources.yml has no source for {name}"


def test_three_way_match_tolerances_are_configuration() -> None:
    """A control's sensitivity must be reviewable, not buried in SQL."""
    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    project = yaml.safe_load((root / "warehouse" / "dbt" / "dbt_project.yml").read_text())
    variables = project["vars"]
    # Quantity is absolute and zero: a part either arrived or it did not.
    assert variables["three_way_match_quantity_tolerance"] == 0
    # Price is a percentage, because a cent on a cable is noise and a cent on a
    # GPU tray is not.
    assert 0 < variables["three_way_match_price_tolerance_pct"] < 0.05


def _unused(value: Any) -> None:  # pragma: no cover - keeps linters quiet
    return None
