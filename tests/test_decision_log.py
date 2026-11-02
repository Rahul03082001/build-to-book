"""The decision log is audit evidence, so its structure is enforced.

An ADR that silently loses its "Options considered" section stops being
evidence that the choice was deliberate. These tests keep the log honest:
contiguous numbering, an index that matches the entries, and every section of
the agreed template present in every entry.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

# The 19 decisions settled up front are a floor, not a total -- later phases
# add entries. Contiguous numbering and the index-match test are what guard
# the file's structure, so a hardcoded total would only create test churn.
MINIMUM_ADR_COUNT = 19

REQUIRED_SECTIONS = [
    "### Context",
    "### Options considered",
    "### Decision",
    "### Reasons",
    "### Consequences",
    "### How a reviewer can verify it",
]

VALID_STATUSES = ("Proposed", "Accepted")
VALID_AREAS = {"Data", "Modeling", "Controls", "Lineage", "Tooling", "Metrics"}

ADR_HEADING = re.compile(r"^## (ADR-(\d{3})): (.+)$", re.MULTILINE)


@pytest.fixture(scope="module")
def decisions_text(repo_root: pathlib.Path) -> str:
    return (repo_root / "docs" / "DECISIONS.md").read_text()


@pytest.fixture(scope="module")
def entries(decisions_text: str) -> list[tuple[str, str, str]]:
    """(id, number, title) for every ADR heading, in file order."""
    return [(m.group(1), m.group(2), m.group(3)) for m in ADR_HEADING.finditer(decisions_text)]


@pytest.fixture(scope="module")
def bodies(decisions_text: str) -> dict[str, str]:
    """ADR id -> the text of that entry."""
    matches = list(ADR_HEADING.finditer(decisions_text))
    out: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(decisions_text)
        out[match.group(1)] = decisions_text[match.start() : end]
    return out


def test_at_least_the_settled_decisions_are_recorded(
    entries: list[tuple[str, str, str]],
) -> None:
    assert len(entries) >= MINIMUM_ADR_COUNT, (
        f"only {len(entries)} ADRs; the 19 settled decisions must all be present"
    )


def test_numbering_is_contiguous_and_ordered(entries: list[tuple[str, str, str]]) -> None:
    """Append-only numbering: no gaps, no reordering, no reuse."""
    numbers = [int(number) for _, number, _ in entries]
    assert numbers == list(range(1, len(numbers) + 1)), f"numbering is not contiguous: {numbers}"


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_every_entry_has_every_template_section(bodies: dict[str, str], section: str) -> None:
    missing = [adr_id for adr_id, body in bodies.items() if section not in body]
    assert not missing, f"{section!r} missing from: {missing}"


def test_every_entry_declares_date_status_jira_and_area(bodies: dict[str, str]) -> None:
    for adr_id, body in bodies.items():
        assert re.search(r"- Date: \d{4}-\d{2}-\d{2}", body), f"{adr_id} has no valid date"
        assert re.search(r"- Status: ", body), f"{adr_id} has no status"
        assert re.search(r"- Jira: BTB-\d+", body), f"{adr_id} has no Jira key"
        area = re.search(r"- Area: (\w+)", body)
        assert area, f"{adr_id} has no area"
        assert area.group(1) in VALID_AREAS, f"{adr_id} has unknown area {area.group(1)!r}"


def test_statuses_are_valid(bodies: dict[str, str]) -> None:
    for adr_id, body in bodies.items():
        status = re.search(r"- Status: (.+)", body).group(1).strip()
        valid = status in VALID_STATUSES or re.fullmatch(r"Superseded by ADR-\d{3}", status)
        assert valid, f"{adr_id} has invalid status {status!r}"


def test_every_entry_considers_at_least_two_options(bodies: dict[str, str]) -> None:
    """A decision with one option was not a decision."""
    for adr_id, body in bodies.items():
        section = body.split("### Options considered")[1].split("### Decision")[0]
        options = re.findall(r"^\d+\. ", section, re.MULTILINE)
        assert len(options) >= 2, f"{adr_id} lists {len(options)} option(s); at least 2 required"


def test_index_table_matches_the_entries(
    decisions_text: str, entries: list[tuple[str, str, str]]
) -> None:
    """The index is how a reviewer navigates; a stale index hides an entry."""
    index_rows = re.findall(
        r"^\| \[(ADR-\d{3})\]\(#[^)]+\) \| ([\d-]+) \| (.+?) \| (.+?) \|$",
        decisions_text,
        re.MULTILINE,
    )
    assert len(index_rows) == len(entries), (
        f"index has {len(index_rows)} rows but the file has {len(entries)} entries"
    )
    assert [row[0] for row in index_rows] == [adr_id for adr_id, _, _ in entries], (
        "index order does not match entry order"
    )


def test_adr_011_lists_its_compensating_controls(bodies: dict[str, str]) -> None:
    """The segregation-of-duties gap is only acceptable because these exist."""
    body = bodies["ADR-011"]
    assert "### Compensating controls" in body
    for expected in ("Branch protection", "status checks", "checklist", "second approver"):
        assert expected.lower() in body.lower(), f"ADR-011 does not mention {expected!r}"


def test_adr_015_records_free_tier_limits_with_a_check_date(bodies: dict[str, str]) -> None:
    """Free-tier limits change, so the numbers are worthless without a date."""
    body = bodies["ADR-015"]
    assert re.search(r"checked \d{4}-\d{2}-\d{2}", body), (
        "ADR-015 does not record when the limits were checked"
    )
    for provider in ("Neon", "Streamlit", "Tableau Public", "GitHub Pages"):
        assert provider in body, f"ADR-015 does not record limits for {provider}"


def _tracked_markdown(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Markdown files under version control.

    Falls back to a glob before `git init`, excluding the gitignored build
    prompt so the suite behaves the same either way.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.md"],
            cwd=repo_root,
            capture_output=True,
            check=True,
            text=True,
        )
        paths = [repo_root / name for name in result.stdout.split("\0") if name]
        if paths:
            return sorted(paths)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    ignored = {"BUILD_PROMPT.md"}
    return sorted(
        p
        for p in [*repo_root.glob("*.md"), *repo_root.glob("docs/**/*.md")]
        if p.name not in ignored
    )


def test_no_sox_compliance_claim_anywhere(repo_root: pathlib.Path) -> None:
    """ADR-019: the project never claims SOX compliance.

    The risk is an *affirmative* claim ("this pipeline is SOX compliant"), not
    the phrase appearing at all -- ADR-019 and PROJECT_OVERVIEW.md both have to
    name the claim in order to reject it, and DECISIONS.md lists it as the
    option that lost. So this matches claim-shaped constructions only, and
    scans the files that actually ship.
    """
    affirmative = re.compile(
        r"""(?:
              # "is/are/becomes/achieves ... SOX compliant", with no negation
              \b(?:is|are|was|were|becomes|achieves|achieved|provides|ensures
                 |guarantees|delivers|demonstrates)\s+
              (?:fully\s+|completely\s+|now\s+)?
              sox[\s-]?complian(?:t|ce)
            |
              # "a SOX-compliant <thing>"
              \bsox[\s-]?compliant\s+
              (?:pipeline|project|warehouse|system|platform|controls?|process|repo\w*)
            |
              # "SOX compliance achieved"
              \bsox[\s-]?compliance\s+(?:achieved|attained|certified|verified)
            )""",
        re.IGNORECASE | re.VERBOSE,
    )

    # Only files under version control. BUILD_PROMPT.md is gitignored working
    # material, not a published claim.
    tracked = _tracked_markdown(repo_root)

    offenders: list[str] = []
    for path in tracked:
        # Collapse wrapped lines: a claim split across a line break is still a
        # claim, and a negation on the previous line still negates it.
        raw = path.read_text()
        flat = re.sub(r"\s+", " ", raw)
        for match in affirmative.finditer(flat):
            snippet = flat[max(0, match.start() - 90) : match.end() + 20]
            # Discussion of the claim, rather than the claim itself.
            if re.search(
                r"\b(?:not|never|nothing|without|cannot|rather than|instead of"
                r"|would|claiming|overclaim\w*|false|misleading|does not|do not)\b",
                snippet,
                re.IGNORECASE,
            ):
                continue
            offenders.append(f"{path.relative_to(repo_root)}: ...{snippet.strip()}...")

    assert not offenders, "SOX compliance appears to be claimed in:\n" + "\n".join(offenders)


def test_synthetic_and_simulated_labelling_is_present(repo_root: pathlib.Path) -> None:
    """ADR-019 requires the label on every published surface, not just the ADR."""
    for relative in ("README.md", "PROJECT_OVERVIEW.md", "docs/architecture.md"):
        text = (repo_root / relative).read_text().lower()
        assert "synthetic" in text, f"{relative} does not state the data is synthetic"
        assert "simulated" in text, f"{relative} does not state the controls are simulated"
