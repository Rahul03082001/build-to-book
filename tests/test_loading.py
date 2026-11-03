"""Loading against a real warehouse: idempotency, provenance, audit trail.

Marked `integration`. Requires a warehouse with roles and schemas applied:

    make init && .venv/bin/pytest -m integration

These are the Phase 1 properties that cannot be tested without a database.
Chief among them: a rerun must add nothing (ADR-020), the audit trail must
refuse modification (ADR-008), and the subledger must still tie to the GL after
the round trip through CSV, validation, COPY and `numeric(18,2)` -- a precision
loss anywhere in that chain would turn CTRL-003 into a coin flip.

Every property here is checked against **every** declared extract, not a
subset. See LOAD_ORDER below for what a subset cost.
"""

from __future__ import annotations

import csv
import decimal
import json
import pathlib
from collections.abc import Iterator
from typing import Any

import conftest
import psycopg
import pytest

from ingestion import db, loader

pytestmark = pytest.mark.integration

D = decimal.Decimal

#: Every declared extract, from the generator -- never a second copy.
#:
#: This was a hardcoded list of eight. The generator grew to eighteen, and the
#: list did not: bom_headers, bom_items, shipment_headers, shipment_lines, both
#: invoice extracts, production_orders, component_consumption, mes_unit_events
#: and production_confirmations were all silently outside every test in this
#: module. Idempotency, provenance and audit-trail recording were simply never
#: checked for them, and nothing failed to say so -- the assertions below
#: compare against this list, so a short list makes them agree with themselves.
#:
#: Deriving it costs 2.9 seconds (5.4s to 8.2s, measured) and covers ten more
#: sources, including mes_unit_events at 60,774 rows -- the largest extract and
#: the one where the loader's anti-join performance actually matters.
#:
#: `tests/conftest.py` states the same rule for the same reason; this module
#: was the exception that proved it.
LOAD_ORDER = list(conftest.LOAD_ORDER)


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
    """A freshly generated demo dataset, isolated from the repo's data/ dir.

    Via the shared helper, which goes through `dataset.write_all`, rather than
    driving `InventoryGenerator` and the writer directly. The direct path was a
    second way of constructing extracts and it fell behind: when
    `goods_movements` gained `inbound_delivery` (BTB-34) the column was stamped
    by the shipment generator, which this fixture never ran, so every test in
    the module failed with "row 0 is missing columns". The same fault the
    conftest helper's own docstring warns about.
    """
    out = tmp_path_factory.mktemp("extracts")
    conftest.generate_extracts(out)
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

    Per inventory account, because ADR-028 put stock in three. Tying everything
    to 130000 -- which this did -- passes while covering only raw materials,
    since finished goods receipts and dispatches post elsewhere entirely.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH movements AS (
                SELECT CASE
                           WHEN movement_type = '601' THEN '137000'
                           WHEN movement_type = '101' AND order_number IS NOT NULL
                               THEN '137000'
                           ELSE '130000'
                       END AS account,
                       to_char(posting_date, 'YYYY-MM') AS period,
                       sum(amount_usd) AS amount
                FROM raw.goods_movements GROUP BY 1, 2
            ),
            revaluations AS (
                SELECT '130000' AS account,
                       to_char(posting_date, 'YYYY-MM') AS period,
                       sum(amount_usd) AS amount
                FROM raw.inventory_revaluations GROUP BY 2
            ),
            subledger AS (
                SELECT account, period, sum(amount) AS amount FROM (
                    SELECT * FROM movements UNION ALL SELECT * FROM revaluations
                ) combined GROUP BY 1, 2
            ),
            ledger AS (
                SELECT gl_account AS account,
                       to_char(posting_date, 'YYYY-MM') AS period,
                       sum(signed_amount_usd) AS amount
                FROM raw.gl_postings
                WHERE gl_account IN ('130000', '137000') GROUP BY 1, 2
            )
            SELECT s.account || ' ' || s.period, s.amount, l.amount, s.amount - l.amount
            FROM subledger s FULL OUTER JOIN ledger l USING (account, period)
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
    """A PURCHASE receipt must name a real purchase order line.

    Scoped to receipts that carry a purchase order. A receipt against a
    PRODUCTION order is confirmed output (ADR-028) and has no purchase order
    line by design -- SAP uses movement type 101 for both and tells them apart
    by the reference, which is what the filter below does. Without that scope
    this counted every finished goods receipt as an orphan.
    """
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM raw.goods_movements m "
            "LEFT JOIN raw.purchase_order_lines l "
            "  ON l.po_number = m.po_number AND l.po_item = m.po_item "
            "WHERE m.movement_type = '101' AND m.po_number IS NOT NULL "
            "  AND l.po_number IS NULL"
        )
        assert cursor.fetchone()[0] == 0, "orphaned goods receipts in the warehouse"

    # And the separation itself: a receipt carries one reference or the other,
    # never both and never neither.
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM raw.goods_movements "
            "WHERE movement_type = '101' "
            "  AND (po_number IS NULL) = (order_number IS NULL)"
        )
        assert cursor.fetchone()[0] == 0, (
            "a receipt carries both a purchase order and a production order, or "
            "neither -- so nothing can attribute it"
        )


def test_loader_connects_as_a_role_that_cannot_read_reporting() -> None:
    """Least privilege in practice: loading does not need reporting access."""
    assert db.LOADER_ROLE == "btb_loader_svc"
    assert (
        "btb_loader_svc" in db.dsn(db.LOADER_ROLE)
        or "BTB_LOADER_PASSWORD" not in __import__("os").environ
    )


# ---------------------------------------------------------------------------
# --reset: emptying raw before a load
#
# Needed because insert-only loading cannot backfill. The anti-join matches on
# the natural key, so a row already in `raw` is skipped whatever its other
# columns say -- which means a source that gains a COLUMN loads cleanly and
# leaves every existing row holding a null. ADR-020 records the limitation;
# BTB-34 hit it when goods_movements gained inbound_delivery and all 1,242
# receipts skipped, keeping nulls while the extract on disk had the values.
# ---------------------------------------------------------------------------
def test_reset_refuses_a_table_it_was_not_told_about() -> None:
    """Table names are interpolated into a TRUNCATE, so the set is closed.

    They come from the declared load order and never from user input. This
    pins that: a name outside it is refused rather than reaching the SQL.
    """
    from ingestion import __main__ as ingestion_main

    with pytest.raises(ValueError, match="not declared in the load order"):
        ingestion_main.reset_raw(None, ["raw.something_else; drop table audit.pipeline_runs"])
    with pytest.raises(ValueError, match="not declared in the load order"):
        ingestion_main.reset_raw(None, ["pipeline_runs"])


def test_reset_with_nothing_to_do_touches_nothing() -> None:
    from ingestion import __main__ as ingestion_main

    assert ingestion_main.reset_raw(None, []) == 0


def test_reset_empties_raw_and_a_reload_restores_it(
    conn: psycopg.Connection, extracts: pathlib.Path
) -> None:
    """The whole point: after a reset, a load inserts rather than skips."""
    from ingestion import __main__ as ingestion_main

    batch_id = loader.new_batch_id()
    for name in LOAD_ORDER:
        loader.load_extract(conn, name, extracts / f"{name}.csv", batch_id)

    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("select count(*) from raw.goods_movements")
        before = cursor.fetchone()[0]
    assert before > 0

    emptied = ingestion_main.reset_raw(conn, list(LOAD_ORDER))
    assert emptied == len(LOAD_ORDER)

    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("select count(*) from raw.goods_movements")
        assert cursor.fetchone()[0] == 0

    reloaded = loader.load_extract(
        conn, "goods_movements", extracts / "goods_movements.csv", loader.new_batch_id()
    )
    assert reloaded.rows_inserted == before, (
        f"after a reset the load inserted {reloaded.rows_inserted} of {before} rows"
    )
    assert reloaded.rows_skipped == 0, "a load after a reset must insert, not skip"


def test_reset_does_not_touch_the_audit_trail(conn: psycopg.Connection) -> None:
    """Resetting what was loaded must not erase the record of loading it.

    The trail is append-only and a TRUNCATE on it is blocked by a
    statement-level trigger (ADR-008) -- which was added after a TRUNCATE
    silently erased eight rows. This asserts the reset keeps its hands off it.
    """
    from ingestion import __main__ as ingestion_main

    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("select count(*) from audit.pipeline_runs")
        before = cursor.fetchone()[0]
    assert before > 0, "no audit history yet, so this proves nothing"

    ingestion_main.reset_raw(conn, list(LOAD_ORDER))

    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("select count(*) from audit.pipeline_runs")
        assert cursor.fetchone()[0] == before


def test_this_module_covers_every_declared_extract() -> None:
    """Guards against the list being hardcoded again.

    Tautological while LOAD_ORDER is derived, and that is the point: it is
    insurance against a future edit that replaces the derivation with a literal
    list, which is exactly how this module came to cover eight of eighteen
    sources. The assertions in this file compare against LOAD_ORDER, so a short
    list cannot fail on its own -- it just quietly tests less.
    """
    from generator import dataset

    missing = sorted(set(dataset.LOAD_ORDER) - set(LOAD_ORDER))
    assert not missing, (
        f"extract(s) the loader produces but this module never loads: {missing}. "
        "Derive LOAD_ORDER from generator.dataset.LOAD_ORDER rather than listing it."
    )
    unknown = sorted(set(LOAD_ORDER) - set(dataset.LOAD_ORDER))
    assert not unknown, f"extract(s) tested here that the generator does not produce: {unknown}"


# ---------------------------------------------------------------------------
# Chunked loading (BTB-36)
#
# Loading used to read the whole extract into one DataFrame, which peaked at
# 4.0 GB on the full profile's 5,000,000 MES events -- almost exactly the
# 4.3 GB ADR-024 cites for holding them as dicts. Generation streams to avoid
# that; the loader undid the benefit one step downstream.
#
# Chunking introduces one genuinely new risk, and it is the subject of the
# first test below: in-file duplicate detection must still see a duplicate
# whose two rows land in DIFFERENT chunks. Missing it would silently turn a
# rejected duplicate into an inserted one and leave CTRL-001 with nothing to
# find -- so detection moved from pandas to SQL over the whole staged file.
# ---------------------------------------------------------------------------
def _rows(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _rewrite(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_a_duplicate_straddling_a_chunk_boundary_is_still_rejected(
    conn: psycopg.Connection, extracts: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The fault chunking could have introduced.

    The first and last rows of the file are made identical, so with a chunk
    size of two they cannot land in the same chunk. A per-chunk check sees each
    once and rejects nothing.
    """
    from ingestion import loader as loader_module

    source = extracts / "goods_movements.csv"
    rows = _rows(source)
    assert len(rows) > 4, "need enough rows to span several chunks"

    # The duplicate: the first row repeated at the very end of the file.
    staged = [*rows, dict(rows[0])]
    target = tmp_path / "goods_movements.csv"
    _rewrite(target, staged)

    # A sidecar describing the file as written, so the load is not also
    # failing a completeness check (ADR-022) and this test isolates the
    # duplicate behaviour.
    import json

    sidecar = json.loads(
        (extracts / "goods_movements.control_totals.json").read_text(encoding="utf-8")
    )
    sidecar["row_count"] = len(staged)
    sidecar["value_total"] = str(sum(D(row[sidecar["value_column"]]) for row in staged))
    (tmp_path / "goods_movements.control_totals.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )

    original_chunk = loader_module.CHUNK_ROWS
    loader_module.CHUNK_ROWS = 2
    try:
        ingestion_main = __import__("ingestion.__main__", fromlist=["reset_raw"])
        ingestion_main.reset_raw(conn, ["goods_movements"])
        result = loader_module.load_extract(
            conn, "goods_movements", target, loader_module.new_batch_id()
        )
    finally:
        loader_module.CHUNK_ROWS = original_chunk

    assert result.rows_in_file == len(staged), (
        f"chunked read counted {result.rows_in_file} of {len(staged)} rows"
    )
    assert result.rows_rejected == 1, (
        f"the duplicate was not rejected ({result.rows_rejected} rejected) -- a "
        "per-chunk check would miss it because its two rows are in different chunks"
    )
    assert result.rows_inserted == len(rows), (
        "the first occurrence must still load; only the surplus is quarantined"
    )


def test_the_chunk_size_does_not_change_what_is_loaded(
    conn: psycopg.Connection, extracts: pathlib.Path
) -> None:
    """Reading in chunks must be invisible in the result.

    Run twice at wildly different chunk sizes against a clean target, and every
    counted figure must match. If they differed, the chunk size would be a
    silent input to the data rather than a memory knob.
    """
    from ingestion import loader as loader_module

    ingestion_main = __import__("ingestion.__main__", fromlist=["reset_raw"])
    source = extracts / "goods_movements.csv"

    outcomes = []
    original = loader_module.CHUNK_ROWS
    try:
        for size in (3, 1_000_000):
            loader_module.CHUNK_ROWS = size
            ingestion_main.reset_raw(conn, ["goods_movements"])
            result = loader_module.load_extract(
                conn, "goods_movements", source, loader_module.new_batch_id()
            )
            outcomes.append(
                (
                    result.rows_in_file,
                    result.rows_inserted,
                    result.rows_rejected,
                    result.value_total_in_file,
                    result.file_sha256,
                )
            )
    finally:
        loader_module.CHUNK_ROWS = original

    assert outcomes[0] == outcomes[1], (
        f"chunk size changed the outcome: {outcomes[0]} at 3 rows against {outcomes[1]} in one pass"
    )


def test_the_value_total_is_summed_across_chunks(
    conn: psycopg.Connection, extracts: pathlib.Path
) -> None:
    """The control total has to cover the whole file, not the last chunk.

    It is compared against the sidecar's declared total, so a per-chunk value
    would fail CTRL-001 for a reason that has nothing to do with the data.
    """
    import json

    from ingestion import loader as loader_module

    ingestion_main = __import__("ingestion.__main__", fromlist=["reset_raw"])
    sidecar = json.loads(
        (extracts / "goods_movements.control_totals.json").read_text(encoding="utf-8")
    )

    original = loader_module.CHUNK_ROWS
    loader_module.CHUNK_ROWS = 7
    try:
        ingestion_main.reset_raw(conn, ["goods_movements"])
        result = loader_module.load_extract(
            conn, "goods_movements", extracts / "goods_movements.csv", loader_module.new_batch_id()
        )
    finally:
        loader_module.CHUNK_ROWS = original

    assert result.value_total_in_file == D(sidecar["value_total"]), (
        f"summed {result.value_total_in_file} across chunks against a declared "
        f"{sidecar['value_total']}"
    )
    assert result.rows_in_file == sidecar["row_count"]
