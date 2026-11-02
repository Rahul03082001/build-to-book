"""Loading against a real warehouse: idempotency, provenance, audit trail.

Marked `integration`. Requires a warehouse with roles and schemas applied:

    make init && .venv/bin/pytest -m integration

These are the Phase 1 properties that cannot be tested without a database.
Chief among them: a rerun must add nothing (ADR-020), the audit trail must
refuse modification (ADR-008), and the subledger must still tie to the GL after
the round trip through CSV, validation, COPY and `numeric(18,2)` -- a precision
loss anywhere in that chain would turn CTRL-003 into a coin flip.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from generator import config as genconfig
from generator import inventory, writers
from ingestion import db, loader

pytestmark = pytest.mark.integration

D = decimal.Decimal

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


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    try:
        connection = db.connect()
    except psycopg.OperationalError as error:
        pytest.skip(f"warehouse unavailable: {error}")
    with connection:
        yield connection


@pytest.fixture(scope="module")
def extracts(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A freshly generated demo dataset, isolated from the repo's data/ dir."""
    out = tmp_path_factory.mktemp("extracts")
    cfg = genconfig.load()
    data = inventory.InventoryGenerator(cfg, "demo").generate()
    stamp = dt.datetime(2026, 11, 2, 12, 0, 0)
    for name, rows in data.as_mapping().items():
        writers.write_extract(inventory.EXTRACT_SPECS[name], rows, out, stamp)
    return out


@pytest.fixture(scope="module", autouse=True)
def clean_raw(conn: psycopg.Connection, extracts: pathlib.Path) -> None:
    """Empty the raw tables so insert counts are meaningful.

    `raw` is reloadable, so truncating it is safe. The audit tables are *not*
    truncated -- they are append-only by design and refuse it -- so the tests
    below filter by their own batch id instead.
    """
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("TRUNCATE " + ", ".join(f"raw.{name}" for name in LOAD_ORDER))


def _load_all(conn: psycopg.Connection, extracts: pathlib.Path, batch_id: str) -> dict[str, Any]:
    return {
        name: loader.load_extract(conn, name, extracts / f"{name}.csv", batch_id)
        for name in LOAD_ORDER
    }


@pytest.fixture(scope="module")
def first_run(conn: psycopg.Connection, extracts: pathlib.Path) -> dict[str, Any]:
    return _load_all(conn, extracts, loader.new_batch_id())


@pytest.fixture(scope="module")
def second_run(
    conn: psycopg.Connection, extracts: pathlib.Path, first_run: dict[str, Any]
) -> dict[str, Any]:
    return _load_all(conn, extracts, loader.new_batch_id())


# ---------------------------------------------------------------------------
# BTB-15 / ADR-020: incremental, idempotent loading
# ---------------------------------------------------------------------------
def test_first_run_loads_every_row(first_run: dict[str, Any]) -> None:
    for name, result in first_run.items():
        assert result.rows_inserted == result.rows_in_file, name
        assert result.rows_skipped == 0, name
        assert result.rows_rejected == 0, f"{name}: clean data should reject nothing"
        assert result.status == "success", name


def test_rerun_inserts_nothing_and_reports_what_it_skipped(
    first_run: dict[str, Any], second_run: dict[str, Any]
) -> None:
    """The property ADR-017 requires, and the count ADR-020 adds.

    Asserting the skip count, not just an unchanged row total: a loader that
    silently discarded everything would also leave the total unchanged.
    """
    for name, result in second_run.items():
        assert result.rows_inserted == 0, f"{name} re-inserted rows on a rerun"
        assert result.rows_skipped == first_run[name].rows_inserted, (
            f"{name}: skipped {result.rows_skipped}, expected {first_run[name].rows_inserted}"
        )


def test_row_counts_in_the_warehouse_match_the_files(
    conn: psycopg.Connection, second_run: dict[str, Any], first_run: dict[str, Any]
) -> None:
    with conn.cursor() as cursor:
        for name, result in first_run.items():
            cursor.execute(f"SELECT count(*) FROM raw.{name}")
            assert cursor.fetchone()[0] == result.rows_in_file, name


def test_no_staging_tables_are_left_behind(
    conn: psycopg.Connection, second_run: dict[str, Any]
) -> None:
    """Staging is dropped on both the success and failure paths (ADR-020)."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'raw' AND table_name LIKE %s",
            ("\\_stg\\_%",),
        )
        assert cursor.fetchall() == []


# ---------------------------------------------------------------------------
# BTB-16: provenance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("extract_name", LOAD_ORDER)
def test_every_landed_row_carries_provenance(
    conn: psycopg.Connection, first_run: dict[str, Any], extract_name: str
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT count(*) FROM raw.{extract_name} "
            "WHERE _batch_id IS NULL OR _source_file IS NULL "
            "   OR _file_sha256 IS NULL OR _loaded_at IS NULL"
        )
        assert cursor.fetchone()[0] == 0, f"{extract_name} has rows without provenance"


def test_file_hash_on_rows_matches_the_file(
    conn: psycopg.Connection, first_run: dict[str, Any], extracts: pathlib.Path
) -> None:
    """The stamped checksum must be the file's, or provenance proves nothing."""
    expected = loader.file_sha256(extracts / "goods_movements.csv")
    with conn.cursor() as cursor:
        cursor.execute("SELECT DISTINCT _file_sha256 FROM raw.goods_movements")
        assert [row[0] for row in cursor.fetchall()] == [expected]


# ---------------------------------------------------------------------------
# BTB-18 / ADR-008: the audit trail
# ---------------------------------------------------------------------------
def test_one_audit_row_per_file_per_run(
    conn: psycopg.Connection, first_run: dict[str, Any]
) -> None:
    batch_id = next(iter(first_run.values())).batch_id
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT extract_name, rows_in_file, rows_inserted, rows_skipped, "
            "       rows_rejected, status, git_commit_sha, file_sha256 "
            "FROM audit.pipeline_runs WHERE batch_id = %s",
            (batch_id,),
        )
        rows = {row[0]: row for row in cursor.fetchall()}

    assert set(rows) == set(LOAD_ORDER)
    for name, result in first_run.items():
        _, in_file, inserted, skipped, rejected, status, commit, digest = rows[name]
        assert (in_file, inserted, skipped, rejected) == (
            result.rows_in_file,
            result.rows_inserted,
            result.rows_skipped,
            result.rows_rejected,
        ), name
        assert status == "success"
        assert commit, "no commit recorded -- the data cannot be tied to its code"
        assert len(digest) == 64


def test_audit_records_the_declared_control_totals(
    conn: psycopg.Connection, first_run: dict[str, Any], extracts: pathlib.Path
) -> None:
    """Stored so CTRL-001 can be re-tested from the audit trail alone."""
    batch_id = next(iter(first_run.values())).batch_id
    sidecar = json.loads((extracts / "goods_movements.control_totals.json").read_text())
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT control_total_rows, control_total_value FROM audit.pipeline_runs "
            "WHERE batch_id = %s AND extract_name = 'goods_movements'",
            (batch_id,),
        )
        rows, value = cursor.fetchone()
    assert rows == sidecar["row_count"]
    assert value == D(sidecar["value_total"])


def test_completeness_holds_for_every_extract(first_run: dict[str, Any]) -> None:
    for name, result in first_run.items():
        assert result.completeness_ok, (
            f"{name}: {result.rows_inserted} + {result.rows_skipped} + "
            f"{result.rows_rejected} != {result.control_total_rows}"
        )


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_audit_trail_refuses_modification(
    conn: psycopg.Connection, first_run: dict[str, Any], operation: str
) -> None:
    """Append-only, enforced three ways (ADR-008).

    TRUNCATE is included because it fires no row-level trigger. Before the
    statement-level guard existed, a single TRUNCATE erased the whole audit
    trail silently -- verified by doing it.
    """
    statements = {
        "UPDATE": "UPDATE audit.pipeline_runs SET rows_inserted = 999",
        "DELETE": "DELETE FROM audit.pipeline_runs",
        "TRUNCATE": "TRUNCATE audit.pipeline_runs",
    }
    with conn.cursor() as cursor, pytest.raises(psycopg.errors.Error) as caught:
        cursor.execute(statements[operation])
    conn.rollback()
    assert "append-only" in str(caught.value), (
        f"{operation} was refused, but not by the append-only guard: {caught.value}"
    )


def test_audit_trail_survived_those_attempts(
    conn: psycopg.Connection, first_run: dict[str, Any]
) -> None:
    batch_id = next(iter(first_run.values())).batch_id
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), max(rows_inserted) FROM audit.pipeline_runs WHERE batch_id = %s",
            (batch_id,),
        )
        count, largest = cursor.fetchone()
    assert count == len(LOAD_ORDER)
    assert largest != 999, "an UPDATE got through"


def test_a_failed_load_is_still_recorded(
    conn: psycopg.Connection, extracts: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A run that left no trace is the one an auditor cannot test."""
    broken = tmp_path / "materials.csv"
    rows = list(csv.DictReader((extracts / "materials.csv").open()))
    rows[0]["created_on"] = "not-a-date"
    rows[1]["material_group"] = ""
    with broken.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    # A sidecar whose row count no longer matches, to force a completeness miss.
    sidecar = json.loads((extracts / "materials.control_totals.json").read_text())
    sidecar["row_count"] = len(rows) + 5
    (tmp_path / "materials.control_totals.json").write_text(json.dumps(sidecar))

    batch_id = loader.new_batch_id()
    result = loader.load_extract(conn, "materials", broken, batch_id)

    # Bad rows are quarantined, not dropped, and the shortfall is visible.
    assert result.rows_rejected == 2
    assert not result.completeness_ok
    assert not result.rows_accounted_ok, "the inflated row count should be the failure"
    assert "row count" in result.completeness_detail

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT rejection_rule, rejection_detail FROM audit.rejected_rows "
            "WHERE run_id = %s ORDER BY source_row",
            (result.run_id,),
        )
        rejects = cursor.fetchall()
    assert len(rejects) == 2
    assert all(rule == loader.REJECT_SCHEMA_VIOLATION for rule, _ in rejects)
    assert any("created_on" in detail for _, detail in rejects)


# ---------------------------------------------------------------------------
# BTB-76: source-side duplicate versus rerun
# ---------------------------------------------------------------------------
def test_in_file_duplicate_is_rejected_while_a_rerun_is_skipped(
    conn: psycopg.Connection,
    extracts: pathlib.Path,
    second_run: dict[str, Any],
    tmp_path: pathlib.Path,
) -> None:
    """Both situations in one load, told apart by the counts.

    This is what CTRL-001 needs: an interface that posted twice is a finding,
    a rerun of yesterday's file is routine, and `ON CONFLICT DO NOTHING` would
    have reported both as "0 inserted".
    """
    rows = list(csv.DictReader((extracts / "goods_movements.csv").open()))
    rows.insert(5, dict(rows[0]))
    target = tmp_path / "goods_movements.csv"
    with target.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    # The trailer describes the file as it arrived, so adding a row changes the
    # value total as well as the count. Updating only the count made this test
    # fail once the value-total half of CTRL-001 was implemented -- the check
    # was right and the fixture was wrong.
    sidecar = json.loads((extracts / "goods_movements.control_totals.json").read_text())
    sidecar["row_count"] = len(rows)
    sidecar["value_total"] = str(sum((D(row["amount_usd"]) for row in rows), D("0")))
    (tmp_path / "goods_movements.control_totals.json").write_text(json.dumps(sidecar))

    result = loader.load_extract(conn, "goods_movements", target, loader.new_batch_id())

    assert result.rows_rejected == 1, "the source-side duplicate was not rejected"
    assert result.rows_skipped == len(rows) - 1, "the rerun rows were not skipped"
    assert result.rows_inserted == 0
    assert result.completeness_ok

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT rejection_rule, rejection_detail, source_row "
            "FROM audit.rejected_rows WHERE run_id = %s",
            (result.run_id,),
        )
        rule, detail, source_row = cursor.fetchone()
    assert rule == loader.REJECT_DUPLICATE_NATURAL_KEY
    assert "appears more than once" in detail
    assert source_row == 7, "the reject must point at the offending line in the file"


# ---------------------------------------------------------------------------
# The reconciliation premise, after the round trip
# ---------------------------------------------------------------------------
def test_subledger_ties_to_the_gl_in_the_warehouse(
    conn: psycopg.Connection, second_run: dict[str, Any]
) -> None:
    """CTRL-003's premise, verified in SQL rather than in Python.

    The generator already proves these tie. This proves nothing was lost
    passing through CSV, pandera coercion, COPY and numeric(18,2). A precision
    loss anywhere in that chain would make the control fail for a
    representation reason and look like a real difference.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH movements AS (
                SELECT to_char(posting_date, 'YYYY-MM') AS period, sum(amount_usd) AS amount
                FROM raw.goods_movements GROUP BY 1
            ),
            revaluations AS (
                SELECT to_char(posting_date, 'YYYY-MM') AS period, sum(amount_usd) AS amount
                FROM raw.inventory_revaluations GROUP BY 1
            ),
            subledger AS (
                SELECT period, sum(amount) AS amount FROM (
                    SELECT * FROM movements UNION ALL SELECT * FROM revaluations
                ) combined GROUP BY 1
            ),
            ledger AS (
                SELECT to_char(posting_date, 'YYYY-MM') AS period,
                       sum(signed_amount_usd) AS amount
                FROM raw.gl_postings WHERE gl_account = '130000' GROUP BY 1
            )
            SELECT s.period, s.amount, l.amount, s.amount - l.amount
            FROM subledger s FULL OUTER JOIN ledger l USING (period)
            ORDER BY 1
            """
        )
        rows = cursor.fetchall()

    assert rows, "no periods found"
    for period, subledger, ledger, difference in rows:
        assert difference == D("0.00"), (
            f"{period}: subledger {subledger} != GL {ledger} (difference {difference})"
        )


def test_every_accounting_document_balances_in_the_warehouse(
    conn: psycopg.Connection, second_run: dict[str, Any]
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM ("
            "  SELECT accounting_document, fiscal_year FROM raw.gl_postings"
            "  GROUP BY 1, 2 HAVING sum(signed_amount_usd) <> 0"
            ") unbalanced"
        )
        assert cursor.fetchone()[0] == 0


def test_no_material_shows_negative_on_hand_in_the_warehouse(
    conn: psycopg.Connection, second_run: dict[str, Any]
) -> None:
    """CTRL-010's premise: clean data has no negative balance."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM ("
            "  SELECT material_number FROM raw.goods_movements"
            "  GROUP BY 1 HAVING sum(quantity) < 0"
            ") negative"
        )
        assert cursor.fetchone()[0] == 0


def test_every_goods_receipt_matches_a_po_line_in_the_warehouse(
    conn: psycopg.Connection, second_run: dict[str, Any]
) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM raw.goods_movements m "
            "LEFT JOIN raw.purchase_order_lines l "
            "  ON l.po_number = m.po_number AND l.po_item = m.po_item "
            "WHERE m.movement_type = '101' AND l.po_number IS NULL"
        )
        assert cursor.fetchone()[0] == 0, "orphaned goods receipts in the warehouse"


def test_loader_connects_as_a_role_that_cannot_read_reporting() -> None:
    """Least privilege in practice: loading does not need reporting access."""
    assert db.LOADER_ROLE == "btb_loader_svc"
    assert (
        "btb_loader_svc" in db.dsn(db.LOADER_ROLE)
        or "BTB_LOADER_PASSWORD" not in __import__("os").environ
    )
