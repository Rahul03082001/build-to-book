"""Run the checks and record every result (BTB-22).

Every check writes a row to `audit.reconciliation_results` whether it passed
or failed. Recording the passes matters as much as the failures: a control
whose results only appear when something is wrong cannot be shown to have been
operating, which is the first thing an auditor asks.

The runner never decides what to do about a failure. It records results and
reports them; the publish gate decides whether to block (ADR-010). Keeping
those separate means a failing check can be investigated without the gate's
behaviour being in question, and the same results drive both the gate and the
trust console.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import uuid
from collections.abc import Sequence

import psycopg

from controls.checks import (
    PHASE_1_CHECKS,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    Check,
    CheckShape,
)

__all__ = [
    "ALLOWED_TARGET_SCHEMAS",
    "PHASE_1_CHECKS",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "Check",
    "CheckResult",
    "CheckShape",
    "RunSummary",
    "UnknownTargetSchemaError",
    "run_checks",
]

#: How many offending rows to quote in a failure's detail. Enough to start
#: investigating, not so many that the audit row becomes a data dump.
EXCEPTION_SAMPLE_SIZE = 5

#: Schemas a check may be pointed at. The schema name is interpolated into the
#: SQL rather than bound as a parameter, because a parameter cannot be an
#: identifier. An allowlist keeps that interpolation from being a way to reach
#: anywhere else, even though the value comes from our own configuration today.
ALLOWED_TARGET_SCHEMAS = frozenset({"publish_candidate", "reporting", "sandbox", "marts"})


class UnknownTargetSchemaError(ValueError):
    """Raised when a check is pointed at a schema outside the allowlist."""


@dataclasses.dataclass
class CheckResult:
    """One check's outcome, as recorded."""

    check: Check
    period: str | None
    expected_value: decimal.Decimal | None
    actual_value: decimal.Decimal | None
    difference: decimal.Decimal | None
    exception_count: int | None
    passed: bool
    detail: str | None
    started_at: dt.datetime
    ended_at: dt.datetime

    @property
    def blocks_publish(self) -> bool:
        return not self.passed and self.check.blocks_publish


@dataclasses.dataclass
class RunSummary:
    """Everything one control run produced."""

    run_id: uuid.UUID
    batch_id: str
    target_schema: str
    results: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [result for result in self.results if result.blocks_publish]

    @property
    def warnings(self) -> list[CheckResult]:
        return [
            result
            for result in self.results
            if not result.passed and not result.check.blocks_publish
        ]

    @property
    def checks_run(self) -> int:
        return len(self.results)

    @property
    def checks_passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def blocking_check_ids(self) -> list[str]:
        # Deduplicated and ordered, since several checks can share a control ID.
        seen: dict[str, None] = {}
        for result in self.blocking_failures:
            seen.setdefault(result.check.check_id, None)
        return list(seen)


def _run_comparison(
    cursor: psycopg.Cursor,
    check: Check,
    schema: str,
    batch_id: str,
    started: dt.datetime,
) -> list[CheckResult]:
    """Run a two-figures-must-agree check, one result per period."""
    cursor.execute(check.sql.format(schema=schema), {"batch_id": batch_id})
    rows = cursor.fetchall()

    results: list[CheckResult] = []
    now = dt.datetime.now(dt.UTC)

    if not rows:
        # No periods at all is itself a finding: the check had nothing to
        # compare, which is not the same as agreeing.
        return [
            CheckResult(
                check=check,
                period=None,
                expected_value=None,
                actual_value=None,
                difference=None,
                exception_count=None,
                passed=False,
                detail="the check returned no rows, so nothing was compared",
                started_at=started,
                ended_at=now,
            )
        ]

    for period, expected, actual in rows:
        expected = decimal.Decimal(expected or 0)
        actual = decimal.Decimal(actual or 0)
        difference = actual - expected
        passed = abs(difference) <= check.tolerance
        results.append(
            CheckResult(
                check=check,
                period=period,
                expected_value=expected,
                actual_value=actual,
                difference=difference,
                exception_count=None,
                passed=passed,
                detail=(
                    None
                    if passed
                    else (
                        f"{period}: expected {expected}, got {actual}, "
                        f"difference {difference} (tolerance {check.tolerance})"
                    )
                ),
                started_at=started,
                ended_at=now,
            )
        )
    return results


def _run_exception(
    cursor: psycopg.Cursor,
    check: Check,
    schema: str,
    batch_id: str,
    started: dt.datetime,
) -> list[CheckResult]:
    """Run a these-rows-should-not-exist check. One result, with a sample."""
    cursor.execute(check.sql.format(schema=schema), {"batch_id": batch_id})
    rows = cursor.fetchall()
    count = len(rows)
    now = dt.datetime.now(dt.UTC)

    detail = None
    if count:
        sample = [str(row[0]) for row in rows[:EXCEPTION_SAMPLE_SIZE]]
        detail = f"{count} exception(s); first {len(sample)}: " + " | ".join(sample)
        if count > EXCEPTION_SAMPLE_SIZE:
            detail += f" ... and {count - EXCEPTION_SAMPLE_SIZE} more"

    return [
        CheckResult(
            check=check,
            period=None,
            expected_value=decimal.Decimal(0),
            actual_value=decimal.Decimal(count),
            difference=decimal.Decimal(count),
            exception_count=count,
            passed=count == 0,
            detail=detail,
            started_at=started,
            ended_at=now,
        )
    ]


def run_checks(
    conn: psycopg.Connection,
    target_schema: str,
    batch_id: str,
    run_id: uuid.UUID | None = None,
    checks: Sequence[Check] = PHASE_1_CHECKS,
) -> RunSummary:
    """Run every check against a schema and record the results.

    A check that raises is recorded as a failure rather than crashing the run.
    An unrunnable check is a failed control, not an absent one -- silently
    skipping it would let a broken query masquerade as a pass.

    Args:
        conn: Connection. The caller manages the transaction.
        target_schema: Schema to check -- `publish_candidate` before a
            publish, or `sandbox` for the failure demo.
        batch_id: The batch being checked.
        run_id: Defaults to a fresh UUID.
        checks: Which checks to run.

    Returns:
        Every result, pass and fail.
    """
    if target_schema not in ALLOWED_TARGET_SCHEMAS:
        raise UnknownTargetSchemaError(
            f"{target_schema!r} is not a checkable schema; "
            f"expected one of {sorted(ALLOWED_TARGET_SCHEMAS)}"
        )

    run_id = run_id or uuid.uuid4()
    summary = RunSummary(run_id=run_id, batch_id=batch_id, target_schema=target_schema, results=[])

    for check in checks:
        started = dt.datetime.now(dt.UTC)
        try:
            with conn.transaction(), conn.cursor() as cursor:
                if check.shape is CheckShape.COMPARISON:
                    summary.results.extend(
                        _run_comparison(cursor, check, target_schema, batch_id, started)
                    )
                else:
                    summary.results.extend(
                        _run_exception(cursor, check, target_schema, batch_id, started)
                    )
        except psycopg.Error as error:
            summary.results.append(
                CheckResult(
                    check=check,
                    period=None,
                    expected_value=None,
                    actual_value=None,
                    difference=None,
                    exception_count=None,
                    passed=False,
                    detail=f"the check could not run: {type(error).__name__}: {error}"[:1000],
                    started_at=started,
                    ended_at=dt.datetime.now(dt.UTC),
                )
            )

    record_results(conn, summary)
    return summary


def record_results(conn: psycopg.Connection, summary: RunSummary) -> None:
    """Append every result, passes included.

    Written in its own transaction so results survive whatever the caller does
    next -- a gate that blocks must not roll back the evidence explaining why.
    """
    if not summary.results:
        return
    with conn.transaction(), conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO audit.reconciliation_results "
            "(run_id, batch_id, check_id, check_name, severity, period, "
            " expected_value, actual_value, difference, tolerance, exception_count, "
            " passed, detail, target_schema, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    summary.run_id,
                    summary.batch_id,
                    result.check.check_id,
                    result.check.name,
                    result.check.severity,
                    result.period,
                    result.expected_value,
                    result.actual_value,
                    result.difference,
                    result.check.tolerance,
                    result.exception_count,
                    result.passed,
                    result.detail,
                    summary.target_schema,
                    result.started_at,
                    result.ended_at,
                )
                for result in summary.results
            ],
        )
