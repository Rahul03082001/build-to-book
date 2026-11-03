"""Planted defects must be detected exactly (BTB-26, BTB-27).

The manifest is what makes these tests mean anything. Without it a test can
only assert that a control flagged *something*, which says nothing about
whether it caught everything -- and completeness is the property an auditor
actually asks about. So detection is asserted in both directions: every planted
record is found, and nothing unplanted is flagged.

Marked `integration`, and deliberately structured around **two** module-scoped
scenarios rather than one per test. Each scenario costs a generate, a load and
a dbt build; a fixture per test made the module take minutes and most of those
rebuilds asserted nothing new.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import conftest
import psycopg
import pytest

from controls import checks, runner
from controls import publish as publish_module
from generator import config as genconfig
from generator import inventory
from ingestion import loader

pytestmark = pytest.mark.integration

LOAD_ORDER = [
    "materials",
    "suppliers",
    "standard_costs",
    "purchase_order_headers",
    "purchase_order_lines",
    "goods_movements",
    "inventory_revaluations",
    "gl_postings",
]

STAMP = dt.datetime(2026, 11, 2, 12, 0, 0)
REPO = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers -- shared with the publish-gate module via conftest, so both set up
# the warehouse the same way and neither inherits the other's state.
# ---------------------------------------------------------------------------
LOAD_ORDER = conftest.LOAD_ORDER
_generate = conftest.generate_extracts
_reset_raw = conftest.reset_raw
_load_all = conftest.load_all


def _dbt_build() -> None:
    conftest.dbt_build()


def _scalar(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    """Read one value, committing afterwards.

    A bare `with conn.cursor()` read on a non-autocommit connection opens a
    transaction nothing commits, holding ACCESS SHARE locks for the rest of
    the session. The dbt subprocess below then blocks on them forever, which is
    precisely what happened before this helper existed: `idle in transaction`
    on one connection, `wait_event_type = Lock` on dbt's.
    """
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return row[0] if row else None


@pytest.fixture(scope="module")
def conn(warehouse: psycopg.Connection) -> Iterator[psycopg.Connection]:
    yield warehouse
    # This module loads defective data into the shared `raw` schema and
    # rebuilds the marts from it, so it must restore a clean built state.
    # Truncating raw alone was not enough -- the marts still held the
    # defective build.
    with contextlib.suppress(Exception):
        conftest.establish_clean_warehouse(warehouse)


# ---------------------------------------------------------------------------
# Scenario 1: a clean dataset published, then a defective extract arrives.
#
# This is Phase 1's exit criterion, and it also yields the detection counts for
# both defects -- so most assertions below read from it rather than rebuilding.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def both_defects(
    conn: psycopg.Connection, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("both")

    _generate(root / "clean", ())
    _reset_raw(conn)
    clean_load = _load_all(conn, root / "clean")
    _dbt_build()
    clean_summary = runner.run_checks(conn, "", clean_load["batch_id"])
    if not clean_summary.passed:
        pytest.fail(
            "the clean dataset did not pass its own controls: "
            + "; ".join(r.detail or "" for r in clean_summary.blocking_failures)
        )
    baseline = publish_module.publish(conn, clean_summary, "test")

    value_before = _scalar(
        conn, "SELECT sum(closing_value_usd) FROM reporting.mart_inventory_balance"
    )

    manifest = _generate(root / "dirty", ("duplicate_goods_receipt", "backdated_gl_posting"))
    dirty_load = _load_all(conn, root / "dirty")
    _dbt_build()
    dirty_summary = runner.run_checks(conn, "", dirty_load["batch_id"])
    result = publish_module.publish(conn, dirty_summary, "test")

    return {
        "manifest": manifest,
        "baseline": baseline,
        "value_before": value_before,
        "load": dirty_load,
        "summary": dirty_summary,
        "publish": result,
    }


# ---------------------------------------------------------------------------
# Scenario 2: the backdated posting alone, to show CTRL-006 is specific.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def backdated_only(
    conn: psycopg.Connection, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("backdated")
    manifest = _generate(root, ("backdated_gl_posting",))
    _reset_raw(conn)
    load = _load_all(conn, root)
    _dbt_build()
    summary = runner.run_checks(conn, "", load["batch_id"])
    return {"manifest": manifest, "load": load, "summary": summary}


# ---------------------------------------------------------------------------
# BTB-26: duplicate goods receipt
# ---------------------------------------------------------------------------
def test_duplicate_goods_receipt_is_rejected_exactly(both_defects: dict[str, Any]) -> None:
    """Preventive: the duplicate never lands, so inventory is never overstated.

    Detecting it afterwards would mean the overstatement had already reached a
    report (ADR-020).
    """
    planted = both_defects["manifest"]["counts_by_defect"]["duplicate_goods_receipt"]
    assert planted > 0, "nothing was planted, so this would prove nothing"

    rejected = sum(r.rows_rejected for r in both_defects["load"]["results"].values())
    assert rejected == planted, f"planted {planted} but the loader rejected {rejected}"


def test_every_rejected_duplicate_is_logged_with_its_rule(
    conn: psycopg.Connection, both_defects: dict[str, Any]
) -> None:
    planted = both_defects["manifest"]["counts_by_defect"]["duplicate_goods_receipt"]
    logged = _scalar(
        conn,
        "SELECT count(*) FROM audit.rejected_rows WHERE batch_id = %s AND rejection_rule = %s",
        (both_defects["load"]["batch_id"], loader.REJECT_DUPLICATE_NATURAL_KEY),
    )
    assert logged == planted


def test_the_duplicate_blocks_the_run_rather_than_being_absorbed(
    both_defects: dict[str, Any],
) -> None:
    """CTRL-001's row-count identity still passes -- which is why the reject
    check has to be separate.

    A reject *is* accounted for, so the identity reconciles. But a rejected row
    means the certified dataset came from an extract the source got wrong.
    """
    summary = both_defects["summary"]
    identity = next(r for r in summary.results if r.check is checks.SOURCE_TO_RAW_COMPLETENESS)
    rejects = next(r for r in summary.results if r.check is checks.NO_REJECTED_ROWS)

    assert identity.passed, "the row-count identity should still reconcile"
    assert not rejects.passed, "the reject check should have failed"
    assert "CTRL-001" in {r.check.check_id for r in summary.blocking_failures}


# ---------------------------------------------------------------------------
# BTB-27: backdated GL posting
# ---------------------------------------------------------------------------
def test_backdated_posting_loads_rather_than_being_rejected(
    backdated_only: dict[str, Any],
) -> None:
    """It is an insert, not an edit, and it must actually land.

    An earlier injector mutated `entered_on` on an existing document and the
    loader skipped every one -- insert-only loading cannot see a changed value
    on a row already in raw (ADR-020). Four were planted and zero were
    detectable.
    """
    rejected = sum(r.rows_rejected for r in backdated_only["load"]["results"].values())
    assert rejected == 0, "a backdated posting should load, not be rejected"

    inserted = sum(r.rows_inserted for r in backdated_only["load"]["results"].values())
    planted = backdated_only["manifest"]["counts_by_defect"]["backdated_gl_posting"]
    assert inserted > planted, "the planted rows did not reach the warehouse"


def test_closed_period_control_finds_exactly_the_planted_lines(
    backdated_only: dict[str, Any],
) -> None:
    planted = backdated_only["manifest"]["counts_by_defect"]["backdated_gl_posting"]
    results = [r for r in backdated_only["summary"].results if r.check.check_id == "CTRL-006"]
    assert results, "CTRL-006 did not run"
    result = results[0]
    assert not result.passed, "CTRL-006 did not detect the backdated posting"
    assert result.exception_count == planted, (
        f"planted {planted} backdated lines but CTRL-006 found {result.exception_count}"
    )


def test_the_backdated_posting_trips_only_the_closed_period_control(
    backdated_only: dict[str, Any],
) -> None:
    """Specificity, not just sensitivity.

    The injected document is balanced and posts to consumption and GR/IR rather
    than inventory, so the double-entry and subledger-to-GL checks stay quiet.
    If everything failed, the test would not show that CTRL-006 works.
    """
    failed = {r.check.check_id for r in backdated_only["summary"].blocking_failures}
    assert failed == {"CTRL-006"}, f"expected only CTRL-006, got {sorted(failed)}"


# ---------------------------------------------------------------------------
# The gate: Phase 1's exit criterion
# ---------------------------------------------------------------------------
def test_both_defects_block_the_publish(both_defects: dict[str, Any]) -> None:
    failed = {r.check.check_id for r in both_defects["summary"].blocking_failures}
    assert failed == {"CTRL-001", "CTRL-006"}, (
        f"expected CTRL-001 and CTRL-006, got {sorted(failed)}"
    )

    result = both_defects["publish"]
    assert result.status == publish_module.STATUS_BLOCKED
    assert set(result.blocking_checks) == {"CTRL-001", "CTRL-006"}


def test_reporting_keeps_the_last_good_dataset(
    conn: psycopg.Connection, both_defects: dict[str, Any]
) -> None:
    """Same version, same numbers -- a stale figure with an honest timestamp
    rather than a wrong one with a current timestamp."""
    baseline_version = both_defects["baseline"].published_version
    assert both_defects["publish"].published_version == baseline_version

    with conn.transaction(), conn.cursor() as cursor:
        served = publish_module._currently_served_version(
            cursor, "reporting", "mart_inventory_balance"
        )
    value_after = _scalar(
        conn, "SELECT sum(closing_value_usd) FROM reporting.mart_inventory_balance"
    )

    assert served == baseline_version
    assert value_after == both_defects["value_before"]


def test_the_block_is_recorded_with_both_controls_named(
    conn: psycopg.Connection, both_defects: dict[str, Any]
) -> None:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "SELECT blocking_checks, reason FROM audit.publish_log WHERE publish_id = %s",
            (both_defects["publish"].publish_id,),
        )
        blocking, reason = cursor.fetchone()
    assert set(blocking) == {"CTRL-001", "CTRL-006"}
    assert reason and "critical check" in reason


# ---------------------------------------------------------------------------
# The manifest itself -- no database needed
# ---------------------------------------------------------------------------
def test_a_clean_run_plants_nothing(tmp_path: pathlib.Path) -> None:
    """If a clean run planted anything, every control failure would be ambiguous."""
    manifest = _generate(tmp_path, ())
    assert manifest["total_injected"] == 0
    assert manifest["counts_by_defect"] == {}
    assert manifest["defects_enabled"] == []


def test_requesting_an_unimplemented_defect_raises() -> None:
    """Silently skipping it would make a detection test pass with nothing planted."""
    generator = inventory.InventoryGenerator(genconfig.load(), "demo")
    data = generator.generate()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        generator.inject_defects(data, ("uom_mismatch",))


def test_injection_is_deterministic(tmp_path: pathlib.Path) -> None:
    """The same seed must plant the same records, or the manifest is not evidence."""
    keys = ("duplicate_goods_receipt", "backdated_gl_posting")
    first = _generate(tmp_path / "a", keys)
    second = _generate(tmp_path / "b", keys)
    assert first["records"] == second["records"]


def test_every_planted_record_names_its_natural_key(tmp_path: pathlib.Path) -> None:
    """A manifest entry without a key cannot be matched back to a finding."""
    manifest = _generate(tmp_path, ("duplicate_goods_receipt", "backdated_gl_posting"))
    for record in manifest["records"]:
        assert record["natural_key"], record
        assert record["extract"]
        assert record["note"], "a planted record should say what it simulates"
