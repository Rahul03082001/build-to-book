"""The check registry's contracts -- no database needed.

The control matrix and the check registry must agree in both directions: a
check with no control is untestable by a reviewer, and a control with no check
is coverage that does not exist. Both are worse than a missing control because
both look like coverage.
"""

from __future__ import annotations

import csv
import pathlib
import re

import pytest

from controls import checks

MATRIX = pathlib.Path(__file__).resolve().parent.parent / "docs" / "control_matrix.csv"


@pytest.fixture(scope="module")
def matrix() -> dict[str, dict[str, str]]:
    with MATRIX.open(newline="", encoding="utf-8") as fh:
        return {row["Control ID"]: row for row in csv.DictReader(fh)}


def test_checks_exist() -> None:
    assert checks.PHASE_1_CHECKS


def test_every_check_names_a_control_in_the_matrix(
    matrix: dict[str, dict[str, str]],
) -> None:
    """A check with no control row is a control a reviewer cannot look up."""
    for check in checks.PHASE_1_CHECKS:
        assert check.check_id in matrix, (
            f"{check.name!r} cites {check.check_id}, which is not in the control matrix"
        )


def test_every_check_severity_matches_the_matrix(
    matrix: dict[str, dict[str, str]],
) -> None:
    """Severity decides whether a failure blocks (ADR-010), so the matrix and
    the code must not disagree about it."""
    for check in checks.PHASE_1_CHECKS:
        assert check.severity == matrix[check.check_id]["Severity"], (
            f"{check.check_id}: code says {check.severity}, "
            f"matrix says {matrix[check.check_id]['Severity']}"
        )


def test_every_check_has_a_rationale() -> None:
    """The evidence pack quotes this; "it failed" is not actionable."""
    for check in checks.PHASE_1_CHECKS:
        assert len(check.rationale) > 40, f"{check.check_id} has no usable rationale"


def test_every_check_is_critical_in_phase_one() -> None:
    """Nothing in this slice is advisory.

    A warning-severity check would not block a publish, and every Phase 1
    check guards an integrity property that makes the certified number wrong
    if it fails.
    """
    assert checks.critical_checks() == checks.PHASE_1_CHECKS


def test_tolerances_are_zero() -> None:
    """Both sides of every comparison are two-decimal decimals by
    construction, so any difference is real. A materiality threshold is a
    separate decision (ADR-010)."""
    for check in checks.PHASE_1_CHECKS:
        assert check.tolerance == 0, f"{check.check_id} has a non-zero tolerance"


def test_check_sql_is_parameterised_not_interpolated() -> None:
    """A check reading the audit trail must scope itself to the batch.

    Assessing the whole audit history means one historical failure -- a
    deliberately broken test fixture, say -- blocks every future publish for
    ever, which makes the gate useless rather than strict. That is exactly
    what happened on the first run.
    """
    for check in checks.PHASE_1_CHECKS:
        if "audit.pipeline_runs" in check.sql:
            assert "%(batch_id)s" in check.sql, (
                f"{check.check_id} reads the audit trail without scoping to a batch"
            )


def test_check_sql_targets_the_schema_placeholder() -> None:
    """The same check must run against candidate, sandbox or reporting.

    That is what lets the Phase 6 failure demo run against `sandbox` with the
    identical controls, rather than a second copy that could drift.
    """
    for check in checks.PHASE_1_CHECKS:
        # Audit-trail checks are schema-independent by nature.
        if "audit." in check.sql and "{schema}" not in check.sql:
            continue
        assert "{schema}" in check.sql, (
            f"{check.check_id} hardcodes a schema instead of using {{schema}}"
        )


def test_no_check_references_reporting_directly() -> None:
    """Checks run against the candidate, before anything is published.

    A check that read `reporting` would be validating the dataset it is
    supposed to be gating -- passing because the last good data is fine.
    """
    for check in checks.PHASE_1_CHECKS:
        assert not re.search(r"\breporting\.", check.sql), (
            f"{check.check_id} reads reporting directly"
        )


def test_the_subledger_check_includes_revaluations() -> None:
    """ADR-023: the subledger is movements PLUS revaluations.

    Omitting revaluations makes this check pass while the inventory account
    drifts away from quantity x standard cost.
    """
    sql = checks.SUBLEDGER_TO_GL.sql
    assert "fct_goods_movements" in sql
    assert "fct_inventory_revaluations" in sql


def test_the_independent_invariant_is_a_separate_check() -> None:
    """CTRL-003 cannot detect a valuation error shared by both its sources.

    The quantity-times-cost assertion exists precisely because the
    reconciliation cannot catch that class of fault -- so it must not be
    folded into the same check.
    """
    assert checks.INVENTORY_VALUE_AGREES_WITH_COST in checks.PHASE_1_CHECKS
    assert checks.INVENTORY_VALUE_AGREES_WITH_COST is not checks.SUBLEDGER_TO_GL
    assert "dim_material" in checks.INVENTORY_VALUE_AGREES_WITH_COST.sql
    assert "between" in checks.INVENTORY_VALUE_AGREES_WITH_COST.sql.lower(), (
        "the invariant must value as-of the period end, not at today's cost"
    )


def test_closed_period_control_is_absent_with_a_reason() -> None:
    """CTRL-006 is deliberately not implemented yet.

    Detecting a posting made *into* a closed period needs an entry timestamp
    distinct from the posting date, which the generator does not emit. A check
    that cannot tell a backdated entry from a normal one would be worse than
    none, so it lands with the defect that exercises it (BTB-27).
    """
    assert "CTRL-006" not in {c.check_id for c in checks.PHASE_1_CHECKS}
    source = pathlib.Path(checks.__file__).read_text()
    assert "CTRL-006" in source, "the omission must be documented where someone will look"
