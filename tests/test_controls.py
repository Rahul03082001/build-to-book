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

from controls import checks, runner

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


def test_check_sql_names_the_layer_it_reads() -> None:
    """Every check must name its layer, so a sandbox run reads the sandbox build.

    Two checks hardcoded `intermediate.` and `staging.`, which would have made
    the Phase 6 failure demo assess production data while the workflow claimed
    otherwise. Found by running the demo's own target and seeing 14 models land
    in `staging` and none in `sandbox`.
    """
    placeholders = {"{marts}", "{intermediate}", "{staging}"}
    for check in checks.PHASE_1_CHECKS:
        # Audit-trail checks are schema-independent: the trail is one table,
        # not a rebuilt layer.
        if "audit." in check.sql and not any(p in check.sql for p in placeholders):
            continue
        assert any(p in check.sql for p in placeholders), (
            f"{check.check_id} hardcodes a schema instead of naming its layer"
        )


def test_no_check_hardcodes_a_build_layer() -> None:
    """A literal `marts.` or `staging.` would read production from a sandbox run."""
    import re

    for check in checks.PHASE_1_CHECKS:
        for layer in ("staging", "intermediate", "marts"):
            # Allow the placeholder; refuse the bare name followed by a dot.
            bare = re.search(rf"(?<![{{\w]){layer}\.", check.sql)
            assert bare is None, f"{check.check_id} reads {layer}. directly; use {{{layer}}}"


def test_layer_schemas_mirror_the_dbt_macro() -> None:
    """The controls and dbt must agree about where a build lives.

    If they disagreed, the controls would assess a build nobody made -- and
    pass, because an empty schema has no exceptions.
    """
    assert runner.layer_schemas("") == {
        "staging": "staging",
        "intermediate": "intermediate",
        "marts": "marts",
    }
    assert runner.layer_schemas("sandbox_") == {
        "staging": "sandbox_staging",
        "intermediate": "sandbox_intermediate",
        "marts": "sandbox_marts",
    }


def test_unknown_schema_prefix_is_refused() -> None:
    with pytest.raises(runner.UnknownTargetSchemaError):
        runner.layer_schemas("production_")


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


def test_closed_period_control_is_implemented() -> None:
    """CTRL-006 arrived with BTB-27, once the generator emitted `entered_on`.

    This test replaces one that asserted the control was *absent*, with the
    reason recorded in the code. That earlier test was not wasted: it pinned a
    deliberate omission so it could not be forgotten, and it failed the moment
    the omission was filled -- which is what told me to update it.
    """
    assert "CTRL-006" in {c.check_id for c in checks.PHASE_1_CHECKS}
    control = checks.CLOSED_PERIOD_INTEGRITY
    assert control.severity == checks.SEVERITY_CRITICAL, (
        "a backdated posting restates reported figures, so detecting it after "
        "publishing is too late -- it has to block"
    )
    # The close lag must come from configuration, not be hardcoded in the SQL:
    # the control and the generator cannot be allowed to disagree about when a
    # period locks.
    assert "%(close_lag_days)s" in control.sql
    assert "entered_on" in control.sql
