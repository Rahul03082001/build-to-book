"""The repository layout is part of the contract.

`make all` must rebuild the project from scratch on a clean machine, and a
reviewer must find things where the documentation says they are. These tests
fail when a required file is deleted or moved without the docs following.
"""

from __future__ import annotations

import pathlib
import stat

import pytest
import yaml

REQUIRED_DIRECTORIES = [
    "backlog",
    "generator",
    "ingestion",
    "warehouse/sql",
    "warehouse/dbt",
    "controls",
    "lineage",
    "app",
    "exports",
    "dashboards",
    "evidence",
    "tests",
    ".github/workflows",
    "docs",
    "docs/tuning",
]

REQUIRED_FILES = [
    "README.md",
    "PROJECT_OVERVIEW.md",
    "Makefile",
    "docker-compose.yml",
    "pyproject.toml",
    ".gitignore",
    ".env.example",
    ".pre-commit-config.yaml",
    "backlog/jira_backlog.csv",
    "generator/config.yaml",
    "generator/config.py",
    "generator/defects.py",
    "generator/ids.py",
    "generator/inventory.py",
    "generator/writers.py",
    "generator/__main__.py",
    "warehouse/sql/roles.sql",
    "warehouse/sql/raw.sql",
    "warehouse/sql/audit.sql",
    "ingestion/schemas.py",
    "ingestion/loader.py",
    "ingestion/db.py",
    "ingestion/__main__.py",
    "scripts/psql.sh",
    "scripts/check_commit_message.py",
    "warehouse/dbt/dbt_project.yml",
    "docs/DECISIONS.md",
    "docs/architecture.md",
    "docs/control_matrix.csv",
    "docs/metric_definitions.md",
    "docs/dq_issue_log.md",
    "docs/demo_script.md",
    "docs/branch_protection.md",
    ".github/workflows/ci.yml",
    ".github/workflows/pipeline.yml",
    ".github/workflows/docs.yml",
    ".github/workflows/demo_failure.yml",
]

# Every target named in the project specification, including the ones that are
# still stubs. A stub that disappears is as much a regression as a broken one.
REQUIRED_MAKE_TARGETS = [
    "data",
    "load",
    "build",
    "controls",
    "publish",
    "lineage",
    "export",
    "evidence",
    "test",
    "all",
]


@pytest.mark.parametrize("relative", REQUIRED_DIRECTORIES)
def test_required_directory_exists(repo_root: pathlib.Path, relative: str) -> None:
    assert (repo_root / relative).is_dir(), f"missing directory: {relative}"


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_required_file_exists(repo_root: pathlib.Path, relative: str) -> None:
    path = repo_root / relative
    assert path.is_file(), f"missing file: {relative}"
    assert path.stat().st_size > 0, f"file is empty: {relative}"


@pytest.mark.parametrize("target", REQUIRED_MAKE_TARGETS)
def test_makefile_declares_target(repo_root: pathlib.Path, target: str) -> None:
    makefile = (repo_root / "Makefile").read_text()
    assert f"\n{target}:" in makefile, f"Makefile has no `{target}` target"
    assert f".PHONY: {target}" in makefile, f"`{target}` is not declared .PHONY"


# Targets whose phase has landed. A built target that quietly reverts to a stub
# would otherwise still pass the declaration test above.
BUILT_MAKE_TARGETS = ["data", "load"]


@pytest.mark.parametrize("target", BUILT_MAKE_TARGETS)
def test_built_target_is_not_still_a_stub(repo_root: pathlib.Path, target: str) -> None:
    makefile = (repo_root / "Makefile").read_text()
    body = makefile.split(f"\n{target}:")[1].split("\n.PHONY")[0]
    assert "NOT_YET" not in body, f"`{target}` is built but still calls NOT_YET"


def test_env_example_has_no_real_secrets(repo_root: pathlib.Path) -> None:
    """The template must stay a template.

    A password pasted into .env.example reaches the public repository, since
    only `.env` is gitignored.
    """
    text = (repo_root / ".env.example").read_text()
    for line in text.splitlines():
        if "PASSWORD" in line and "=" in line and not line.strip().startswith("#"):
            _, _, value = line.partition("=")
            assert value.strip() in {"", "change_me_locally"}, (
                f"{line.split('=')[0]} in .env.example looks like a real value"
            )


def test_env_file_is_gitignored(repo_root: pathlib.Path) -> None:
    ignored = (repo_root / ".gitignore").read_text().splitlines()
    assert ".env" in ignored, ".env must be gitignored"


@pytest.mark.parametrize("script", ["check_commit_message.py", "psql.sh"])
def test_scripts_are_executable(repo_root: pathlib.Path, script: str) -> None:
    """A script the Makefile invokes directly must carry its exec bit."""
    path = repo_root / "scripts" / script
    assert path.is_file(), f"missing script: {script}"
    assert path.stat().st_mode & stat.S_IXUSR, f"{script} is not executable"


@pytest.mark.parametrize(
    "workflow",
    ["ci.yml", "pipeline.yml", "docs.yml", "demo_failure.yml"],
)
def test_workflow_is_valid_yaml(repo_root: pathlib.Path, workflow: str) -> None:
    parsed = yaml.safe_load((repo_root / ".github" / "workflows" / workflow).read_text())
    assert parsed.get("jobs"), f"{workflow} declares no jobs"


def test_failure_demo_targets_sandbox_only(repo_root: pathlib.Path) -> None:
    """The failure demo must never be able to touch the certified dataset.

    Two layers: the target schema is pinned in the workflow env, and the first
    step refuses to continue if it is anything but `sandbox`.
    """
    text = (repo_root / ".github" / "workflows" / "demo_failure.yml").read_text()
    assert "BTB_TARGET_SCHEMA: sandbox" in text, "demo_failure.yml does not pin the sandbox schema"
    assert "Refuse to run against reporting" in text, (
        "demo_failure.yml has no guard step refusing a non-sandbox target"
    )


def test_ci_enforces_a_jira_key_on_pull_requests(repo_root: pathlib.Path) -> None:
    """Traceability from change to story is a compensating control (ADR-011)."""
    parsed = yaml.safe_load((repo_root / ".github" / "workflows" / "ci.yml").read_text())
    assert "jira-key" in parsed["jobs"], "ci.yml has no Jira key check"
