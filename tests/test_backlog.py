"""The backlog must import into Jira cleanly and stay internally consistent.

A malformed CSV is only discovered at import time, which is exactly when it is
most annoying. These tests catch it at commit time instead.
"""

from __future__ import annotations

import csv
import pathlib
import re

import pytest

EXPECTED_COLUMNS = [
    "Issue Type",
    "Issue Key",
    "Summary",
    "Description",
    "Epic Name",
    "Epic Link",
    "Labels",
    "Priority",
    "Story Points",
]

# One epic per phase: Phase 0 through Phase 7.
EXPECTED_EPIC_COUNT = 8

VALID_PRIORITIES = {"Highest", "High", "Medium", "Low", "Lowest"}
FIBONACCI = {1, 2, 3, 5, 8, 13, 21}


@pytest.fixture(scope="module")
def rows(repo_root: pathlib.Path) -> list[dict[str, str]]:
    with (repo_root / "backlog" / "jira_backlog.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def epics(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r["Issue Type"] == "Epic"]


@pytest.fixture(scope="module")
def stories(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r["Issue Type"] == "Story"]


def test_columns_match_the_importer_mapping(repo_root: pathlib.Path) -> None:
    with (repo_root / "backlog" / "jira_backlog.csv").open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == EXPECTED_COLUMNS


def test_issue_keys_are_unique(rows: list[dict[str, str]]) -> None:
    keys = [r["Issue Key"] for r in rows]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"duplicate issue keys: {sorted(duplicates)}"


def test_issue_keys_are_well_formed_and_contiguous(rows: list[dict[str, str]]) -> None:
    numbers = []
    for row in rows:
        match = re.fullmatch(r"BTB-(\d+)", row["Issue Key"])
        assert match, f"malformed issue key: {row['Issue Key']!r}"
        numbers.append(int(match.group(1)))
    assert sorted(numbers) == list(range(1, len(numbers) + 1)), "issue numbers are not contiguous"


def test_one_epic_per_phase(epics: list[dict[str, str]]) -> None:
    assert len(epics) == EXPECTED_EPIC_COUNT
    for phase in range(8):
        assert any(f"Phase {phase}:" in e["Summary"] for e in epics), f"no epic for Phase {phase}"


def test_every_epic_has_an_epic_name(epics: list[dict[str, str]]) -> None:
    """Jira's importer needs Epic Name to create the epic itself."""
    missing = [e["Issue Key"] for e in epics if not e["Epic Name"].strip()]
    assert not missing, f"epics with no Epic Name: {missing}"


def test_every_story_links_to_an_epic_in_this_file(
    stories: list[dict[str, str]], epics: list[dict[str, str]]
) -> None:
    epic_keys = {e["Issue Key"] for e in epics}
    unresolved = [s["Issue Key"] for s in stories if s["Epic Link"] not in epic_keys]
    assert not unresolved, f"stories with an unresolved Epic Link: {unresolved}"


def test_epics_do_not_link_to_other_epics(epics: list[dict[str, str]]) -> None:
    nested = [e["Issue Key"] for e in epics if e["Epic Link"].strip()]
    assert not nested, f"epics that link to another epic: {nested}"


def test_every_story_has_acceptance_criteria(stories: list[dict[str, str]]) -> None:
    """A story without acceptance criteria cannot be called done."""
    missing = [s["Issue Key"] for s in stories if "Acceptance criteria:" not in s["Description"]]
    assert not missing, f"stories with no acceptance criteria: {missing}"


def test_acceptance_criteria_have_at_least_three_items(stories: list[dict[str, str]]) -> None:
    thin = [
        s["Issue Key"]
        for s in stories
        if len(re.findall(r"^\* ", s["Description"], re.MULTILINE)) < 3
    ]
    assert not thin, f"stories with fewer than 3 acceptance criteria: {thin}"


def test_every_story_has_a_summary_and_a_valid_priority(stories: list[dict[str, str]]) -> None:
    for story in stories:
        assert story["Summary"].strip(), f"{story['Issue Key']} has no summary"
        assert story["Priority"] in VALID_PRIORITIES, (
            f"{story['Issue Key']} has invalid priority {story['Priority']!r}"
        )


def test_story_points_are_fibonacci(stories: list[dict[str, str]]) -> None:
    for story in stories:
        points = story["Story Points"].strip()
        assert points, f"{story['Issue Key']} has no story points"
        assert int(points) in FIBONACCI, f"{story['Issue Key']} has non-Fibonacci points: {points}"


def test_every_story_is_labelled_with_its_phase(stories: list[dict[str, str]]) -> None:
    unlabelled = [
        s["Issue Key"]
        for s in stories
        if not any(label.startswith("phase-") for label in s["Labels"].split(","))
    ]
    assert not unlabelled, f"stories with no phase label: {unlabelled}"
