"""Every control referenced in code exists in the matrix, and vice versa.

The control matrix is what a reviewer tests against. A control that exists in
code but not in the matrix is untested by the reviewer; one in the matrix but
not in code is a claim with nothing behind it. Both are worse than a missing
control, because both look like coverage.
"""

from __future__ import annotations

import csv
import pathlib
import re

import pytest

from generator import defects

EXPECTED_COLUMNS = [
    "Control ID",
    "Risk addressed",
    "Description",
    "Type",
    "Frequency",
    "Owner",
    "Evidence location",
    "Test procedure",
    "Severity",
    "Status",
    "Reference",
]

VALID_TYPES = {"Preventive", "Detective"}
VALID_SEVERITIES = {"critical", "warning"}


@pytest.fixture(scope="module")
def controls(repo_root: pathlib.Path) -> list[dict[str, str]]:
    with (repo_root / "docs" / "control_matrix.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_columns_are_as_specified(repo_root: pathlib.Path) -> None:
    with (repo_root / "docs" / "control_matrix.csv").open(newline="", encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == EXPECTED_COLUMNS


def test_control_ids_are_unique_and_well_formed(controls: list[dict[str, str]]) -> None:
    ids = [c["Control ID"] for c in controls]
    assert len(ids) == len(set(ids)), "duplicate control IDs"
    for control_id in ids:
        assert re.fullmatch(r"CTRL-\d{3}", control_id), f"malformed control ID: {control_id!r}"


def test_every_control_is_preventive_or_detective(controls: list[dict[str, str]]) -> None:
    """Auditors separate controls that stop a problem from controls that find it."""
    for control in controls:
        assert control["Type"] in VALID_TYPES, (
            f"{control['Control ID']} has invalid type {control['Type']!r}"
        )


def test_both_control_types_are_represented(controls: list[dict[str, str]]) -> None:
    types = {c["Type"] for c in controls}
    assert types == VALID_TYPES, f"only {types} present; both types are expected"


def test_every_control_declares_severity_risk_evidence_and_test(
    controls: list[dict[str, str]],
) -> None:
    for control in controls:
        cid = control["Control ID"]
        assert control["Severity"] in VALID_SEVERITIES, f"{cid} has invalid severity"
        assert len(control["Risk addressed"]) > 30, f"{cid} does not describe a real risk"
        assert control["Evidence location"].strip(), f"{cid} names no evidence location"
        assert len(control["Test procedure"]) > 30, f"{cid} has no usable test procedure"
        assert control["Owner"].strip(), f"{cid} has no owner"


def test_defect_registry_controls_all_exist_in_the_matrix(
    controls: list[dict[str, str]],
) -> None:
    """Every planted defect must map to a control a reviewer can look up."""
    matrix_ids = {c["Control ID"] for c in controls}
    referenced = {spec.expected_control for spec in defects.REGISTRY.values()}
    missing = referenced - matrix_ids
    assert not missing, f"defects reference controls missing from the matrix: {sorted(missing)}"


def test_every_defect_maps_to_a_control(controls: list[dict[str, str]]) -> None:
    unmapped = [k for k, s in defects.REGISTRY.items() if not s.expected_control]
    assert not unmapped, f"defects with no expected control: {unmapped}"


def test_partially_implemented_controls_link_to_an_explaining_adr(
    controls: list[dict[str, str]],
) -> None:
    """A gap is acceptable; an unexplained gap is not (ADR-011)."""
    for control in controls:
        status = control["Status"]
        if "partially" in status.lower() or "not implemented" in status.lower():
            assert re.search(r"ADR-\d{3}", status), (
                f"{control['Control ID']} is partially implemented but cites no ADR: {status!r}"
            )


def test_segregation_of_duties_gap_is_recorded_in_the_matrix(
    controls: list[dict[str, str]],
) -> None:
    """The limitation must appear where a reviewer tests controls, not only in the ADR."""
    change_mgmt = [c for c in controls if "change" in c["Description"].lower()]
    assert change_mgmt, "no change-management control in the matrix"
    assert any("ADR-011" in c["Status"] for c in change_mgmt), (
        "the change-management control does not record the segregation-of-duties gap"
    )
