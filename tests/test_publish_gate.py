"""The publish gate against a real warehouse (BTB-23).

Phase 1's exit criterion: a forced failure must leave the dashboard on its last
good refresh. That is the behaviour these tests pin, and it is the one thing
that makes blocking on a failed check cheap enough that nobody disables the
checks under pressure (ADR-010, ADR-014).

Marked `integration`. Needs a built warehouse:

    make init && make data && make load && make build
    .venv/bin/pytest -m integration
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from controls import publish as publish_module
from controls import runner
from ingestion import db, loader

pytestmark = pytest.mark.integration

D = decimal.Decimal


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    try:
        connection = db.connect()
    except psycopg.OperationalError as error:
        pytest.skip(f"warehouse unavailable: {error}")
    with connection:
        yield connection


@pytest.fixture(scope="module", autouse=True)
def require_built_marts(conn: psycopg.Connection) -> None:
    """Skip, rather than fail obscurely, when the marts have not been built.

    CI ran pytest before `make build` and these tests failed with
    `UndefinedTable` five times over. A skip says what to do instead.
    """
    missing = []
    with conn.cursor() as cursor:
        for mart in (*publish_module.PUBLISHED_MARTS, "fct_gl_postings"):
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (publish_module.BUILD_SCHEMA, mart),
            )
            if cursor.fetchone() is None:
                missing.append(f"{publish_module.BUILD_SCHEMA}.{mart}")
    if missing:
        pytest.skip(f"run `make build` first; missing {', '.join(missing)}")


@pytest.fixture(scope="module")
def batch_id(conn: psycopg.Connection) -> str:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT batch_id FROM audit.pipeline_runs WHERE status = 'success' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
    if row is None:
        pytest.skip("no successful load; run `make load` first")
    return row[0]


def _served_version(conn: psycopg.Connection, mart: str = "mart_inventory_balance") -> str | None:
    with conn.cursor() as cursor:
        return publish_module._currently_served_version(cursor, "reporting", mart)


def _reporting_total(conn: psycopg.Connection) -> decimal.Decimal | None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT sum(closing_value_usd) FROM reporting.mart_inventory_balance")
        return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Clean path
# ---------------------------------------------------------------------------
def test_a_clean_build_publishes(conn: psycopg.Connection, batch_id: str) -> None:
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
    assert summary.passed, "the build is not clean, so the gate cannot be tested: " + "; ".join(
        r.detail or "" for r in summary.blocking_failures
    )

    result = publish_module.publish(conn, summary, loader.git_commit_sha())

    assert result.published
    assert result.published_version == result.candidate_version
    assert result.blocking_checks == []
    assert _served_version(conn) == result.published_version


def test_publishing_twice_in_a_row_works(conn: psycopg.Connection, batch_id: str) -> None:
    """Republishing is routine -- the nightly job retries and people rerun it.

    Naming versions after the batch made this fail with
    `DependentObjectsStillExist`, because the versioned table is the one the
    live view depends on. Versions are per attempt for exactly this reason.
    """
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
    first = publish_module.publish(conn, summary, "test")
    second = publish_module.publish(conn, summary, "test")

    assert first.published and second.published
    assert first.published_version != second.published_version
    assert second.previous_version == first.published_version


def test_only_the_current_and_previous_versions_are_retained(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """Two copies of every mart is already most of a 1 GB free tier."""
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
    for _ in range(3):
        publish_module.publish(conn, summary, "test")

    with conn.cursor() as cursor:
        for mart in publish_module.PUBLISHED_MARTS:
            versions = publish_module._existing_versions(cursor, "reporting", mart)
            assert len(versions) <= publish_module.VERSIONS_RETAINED, (
                f"{mart} has {len(versions)} versions retained: {versions}"
            )


def test_the_reporter_role_can_read_the_published_views(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """The live console connects as `reporter` and nothing else.

    The grant is reapplied on every publish because the view is dropped and
    recreated -- forgetting it would break the dashboard one run later, which
    is the same class of fault that ruled out renaming schemas (ADR-021).
    """
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
    publish_module.publish(conn, summary, "test")

    with conn.cursor() as cursor:
        for mart in publish_module.PUBLISHED_MARTS:
            cursor.execute(
                "SELECT has_table_privilege('btb_reporter', %s, 'SELECT')",
                (f"reporting.{mart}",),
            )
            assert cursor.fetchone()[0] is True, f"reporter cannot read reporting.{mart}"


# ---------------------------------------------------------------------------
# The blocked path -- Phase 1's exit criterion
# ---------------------------------------------------------------------------
def test_a_failed_critical_check_blocks_and_leaves_reporting_untouched(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """The behaviour the whole gate exists for.

    A critical failure must leave `reporting` serving exactly what it served
    before -- same version, same numbers -- so a leader sees a stale figure
    with an honest timestamp rather than a wrong one with a current timestamp.
    """
    # Establish a known-good published state.
    clean = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
    assert clean.passed, "the build must be clean before testing the blocked path"
    publish_module.publish(conn, summary=clean, commit_sha="test")

    version_before = _served_version(conn)
    total_before = _reporting_total(conn)
    assert version_before and total_before

    # Break a critical check by removing one inventory GL line from the build.
    # The row is captured on the way out so it can be put back exactly, rather
    # than rebuilt -- a dbt run inside a test would be slow and would hide
    # whether the restore actually worked.
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS _removed_gl_line")
        cursor.execute(
            "CREATE TEMP TABLE _removed_gl_line AS "
            "WITH removed AS ("
            "  DELETE FROM marts.fct_gl_postings WHERE gl_posting_key = ("
            "    SELECT gl_posting_key FROM marts.fct_gl_postings"
            "    WHERE gl_account = '130000' ORDER BY gl_posting_key LIMIT 1)"
            "  RETURNING *"
            ") SELECT * FROM removed"
        )
        cursor.execute("SELECT count(*) FROM _removed_gl_line")
        assert cursor.fetchone()[0] == 1, "nothing was removed, so nothing is broken"

    try:
        broken = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
        assert broken.blocking_failures, "corrupting the build did not fail any check"

        result = publish_module.publish(conn, broken, "test")

        assert result.status == publish_module.STATUS_BLOCKED
        assert "CTRL-003" in result.blocking_checks
        # Reporting is reported as still serving the previous version, and
        # actually is.
        assert result.published_version == version_before
        assert _served_version(conn) == version_before
        assert _reporting_total(conn) == total_before
    finally:
        # Put the exact row back, and assert the build is clean again -- a
        # restore that silently failed would leave every later test running
        # against corrupted marts.
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute("INSERT INTO marts.fct_gl_postings SELECT * FROM _removed_gl_line")
            cursor.execute("DROP TABLE IF EXISTS _removed_gl_line")
        restored = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id)
        assert restored.passed, "the build was not restored: " + "; ".join(
            r.detail or "" for r in restored.blocking_failures
        )


def test_a_blocked_publish_is_recorded_with_its_reason(
    conn: psycopg.Connection,
) -> None:
    """ "Why is the dashboard showing yesterday?" must have an answer in SQL."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status, blocking_checks, checks_run, checks_passed, reason, "
            "       published_version, candidate_version "
            "FROM audit.publish_log WHERE status = 'blocked' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()

    assert row, "no blocked publish recorded"
    status, blocking, run, passed, reason, published, candidate = row
    assert status == "blocked"
    assert blocking, "a blocked publish must name the checks that blocked it"
    assert passed < run
    assert reason and "critical check" in reason
    # The candidate was not published, so the two versions differ.
    assert candidate != published


def test_the_publish_log_refuses_modification(conn: psycopg.Connection) -> None:
    """Append-only, same three layers as the rest of the audit trail."""
    for statement in (
        "UPDATE audit.publish_log SET status = 'published'",
        "DELETE FROM audit.publish_log",
        "TRUNCATE audit.publish_log",
    ):
        with conn.cursor() as cursor, pytest.raises(psycopg.errors.Error) as caught:
            cursor.execute(statement)
        conn.rollback()
        assert "append-only" in str(caught.value), statement


def test_reconciliation_results_refuse_modification(conn: psycopg.Connection) -> None:
    for statement in (
        "UPDATE audit.reconciliation_results SET passed = true",
        "DELETE FROM audit.reconciliation_results",
        "TRUNCATE audit.reconciliation_results",
    ):
        with conn.cursor() as cursor, pytest.raises(psycopg.errors.Error) as caught:
            cursor.execute(statement)
        conn.rollback()
        assert "append-only" in str(caught.value), statement


# ---------------------------------------------------------------------------
# Results recording
# ---------------------------------------------------------------------------
def test_passing_checks_are_recorded_too(conn: psycopg.Connection, batch_id: str) -> None:
    """A control whose results only appear when something is wrong cannot be
    shown to have been operating -- the first thing an auditor asks."""
    run_id = uuid.uuid4()
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id, run_id=run_id)

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE passed) "
            "FROM audit.reconciliation_results WHERE run_id = %s",
            (run_id,),
        )
        recorded, passed = cursor.fetchone()

    assert recorded == summary.checks_run
    assert passed == summary.checks_passed
    assert passed > 0, "no passing result was recorded"


def test_results_carry_expected_actual_and_difference(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """ "The reconciliation failed" is not actionable; the difference is."""
    run_id = uuid.uuid4()
    runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id, run_id=run_id)

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT expected_value, actual_value, difference, tolerance, severity "
            "FROM audit.reconciliation_results "
            "WHERE run_id = %s AND check_id = 'CTRL-003' AND period IS NOT NULL",
            (run_id,),
        )
        rows = cursor.fetchall()

    assert rows, "the subledger-to-GL check recorded no per-period result"
    for expected, actual, difference, tolerance, severity in rows:
        assert expected is not None and actual is not None
        assert difference == actual - expected
        assert tolerance == 0
        assert severity == "critical"


def test_an_unrunnable_check_is_recorded_as_a_failure(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """A broken query must not masquerade as a pass.

    Skipping an unrunnable check silently would mean a typo in a control's SQL
    looks exactly like the control passing.
    """
    broken = runner.Check(
        check_id="CTRL-003",
        name="deliberately broken check",
        shape=runner.CheckShape.EXCEPTION,
        severity=runner.SEVERITY_CRITICAL,
        rationale="x" * 50,
        sql="select * from {schema}.table_that_does_not_exist",
    )
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id, checks=(broken,))

    assert not summary.passed
    assert len(summary.blocking_failures) == 1
    assert "could not run" in (summary.results[0].detail or "")


def test_an_empty_comparison_is_a_failure_not_a_pass(
    conn: psycopg.Connection, batch_id: str
) -> None:
    """Nothing to compare is not the same as the two sides agreeing."""
    empty = runner.Check(
        check_id="CTRL-003",
        name="comparison over no rows",
        shape=runner.CheckShape.COMPARISON,
        severity=runner.SEVERITY_CRITICAL,
        rationale="x" * 50,
        sql=(
            "select 'p'::text as period, 0::numeric as expected_value, "
            "0::numeric as actual_value from {schema}.fct_goods_movements where false"
        ),
    )
    summary = runner.run_checks(conn, publish_module.BUILD_SCHEMA, batch_id, checks=(empty,))

    assert not summary.passed
    assert "nothing was compared" in (summary.results[0].detail or "")


def test_version_suffix_is_unique_per_attempt() -> None:
    now = dt.datetime(2026, 11, 2, 20, 28, 15, tzinfo=dt.UTC)
    suffixes = {publish_module.version_suffix(now, uuid.uuid4()) for _ in range(50)}
    assert len(suffixes) == 50, "two attempts in the same second collided"
    for suffix in suffixes:
        assert len(suffix) <= 30
        assert suffix.startswith("v20261102T202815_")
