"""The dbt project's contracts, checked without a database.

Three things must hold for the warehouse to be documentable and traceable, and
none of them is enforced by dbt itself:

* every model is described, so `dbt docs` is a usable data dictionary
* every mart states its grain, because a fact whose grain is unstated makes
  every count over it ambiguous
* a certified model names an owner, since the lineage report and the Phase 5
  CI gate are driven off that tag

Also bans `select *` in a model's final projection: sqlglot cannot resolve a
star to source columns, so a certified KPI's lineage would be unresolvable
(ADR-009) and the Phase 5 gate would fail with no obvious cause.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

DBT_DIR = pathlib.Path(__file__).resolve().parent.parent / "warehouse" / "dbt"
MODELS_DIR = DBT_DIR / "models"


def _expected_raw_tables() -> set[str]:
    """The raw tables, from the generator's own extract list.

    Restated here once and it drifted immediately: Phase 2 added two extracts
    and this test failed for the right reason with a confusing message.
    Deriving it means a new source system is covered automatically.
    """
    from generator import dataset

    return set(dataset.LOAD_ORDER)


EXPECTED_RAW_TABLES = _expected_raw_tables()


def _model_files() -> list[pathlib.Path]:
    return sorted(MODELS_DIR.rglob("*.sql"))


def _yaml_documents() -> list[dict]:
    documents = []
    for path in sorted(MODELS_DIR.rglob("*.yml")):
        parsed = yaml.safe_load(path.read_text())
        if parsed:
            documents.append(parsed)
    return documents


def _described_models() -> dict[str, dict]:
    described: dict[str, dict] = {}
    for document in _yaml_documents():
        for model in document.get("models", []) or []:
            described[model["name"]] = model
    return described


def test_models_exist() -> None:
    assert _model_files(), "no dbt models found"


@pytest.mark.parametrize("path", _model_files(), ids=lambda p: p.stem)
def test_every_model_is_described(path: pathlib.Path) -> None:
    """An undescribed model makes `dbt docs` useless as a data dictionary."""
    described = _described_models()
    assert path.stem in described, f"{path.stem} has no YAML description"
    description = described[path.stem].get("description", "")
    assert len(description.strip()) > 20, f"{path.stem}'s description is too thin"


@pytest.mark.parametrize(
    "path",
    [p for p in _model_files() if p.parent.name == "marts"],
    ids=lambda p: p.stem,
)
def test_every_mart_declares_its_grain(path: pathlib.Path) -> None:
    """A fact whose grain is unstated makes every count over it ambiguous."""
    description = _described_models()[path.stem].get("description", "")
    assert "GRAIN:" in description, f"{path.stem} does not declare its grain in its description"


def test_certified_models_name_an_owner() -> None:
    """The lineage report and the Phase 5 CI gate are driven off this tag."""
    certified = []
    for name, model in _described_models().items():
        meta = (model.get("config") or {}).get("meta") or model.get("meta") or {}
        if meta.get("certified"):
            certified.append((name, meta))

    assert certified, "no certified models found; the lineage report would be empty"
    for name, meta in certified:
        assert meta.get("owner"), f"{name} is certified but names no owner"


def test_the_inventory_mart_is_certified() -> None:
    """Phase 1's whole point is one certified KPI, end to end."""
    model = _described_models()["mart_inventory_balance"]
    meta = (model.get("config") or {}).get("meta") or model.get("meta") or {}
    assert meta.get("certified") is True
    assert meta.get("owner") == "Finance"


@pytest.mark.parametrize("path", _model_files(), ids=lambda p: p.stem)
def test_no_model_selects_star_in_its_projection(path: pathlib.Path) -> None:
    """`select *` cannot be resolved to source columns by sqlglot (ADR-009).

    A star in a CTE that reads a source is fine and is how staging models
    start. A star in the model's final projection is not: the Phase 5 lineage
    gate would be unable to trace a certified KPI to its raw columns, and the
    failure would point at the lineage tool rather than at the model.
    """
    sql = path.read_text()
    # Strip comments so a star inside one does not trip the check.
    without_comments = re.sub(r"--[^\n]*", "", sql)
    # The final projection is whatever follows the last top-level `select`.
    final = without_comments.rsplit("select", 1)[-1]
    assert not re.match(r"\s*\*", final), (
        f"{path.stem}'s final projection is `select *`; name the columns so "
        f"lineage can resolve them"
    )


def test_sources_cover_every_raw_table() -> None:
    """A raw table with no source definition cannot be tested or traced."""
    declared: set[str] = set()
    for document in _yaml_documents():
        for source in document.get("sources", []) or []:
            for table in source.get("tables", []) or []:
                declared.add(table["name"])
    assert declared == EXPECTED_RAW_TABLES, (
        f"sources and raw tables disagree: "
        f"missing {EXPECTED_RAW_TABLES - declared}, extra {declared - EXPECTED_RAW_TABLES}"
    )


def test_generate_schema_name_macro_exists() -> None:
    """Without it, `+schema: staging` becomes `publish_candidate_staging`.

    That name sits outside every grant in roles.sql, so the transformer could
    not write to it.
    """
    macro = DBT_DIR / "macros" / "generate_schema_name.sql"
    assert macro.exists()
    assert "custom_schema_name | trim" in macro.read_text()


def test_dbt_project_fails_the_build_on_a_failing_test() -> None:
    """ADR-010: a failing test must block, not warn.

    dbt's default severity is error, but setting it explicitly means a future
    `+severity: warn` has to be a deliberate, reviewable change rather than an
    inherited default nobody notices.
    """
    project = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    assert project["tests"]["build_to_book"]["+severity"] == "error"


def test_singular_tests_exist_for_the_cross_model_invariants() -> None:
    """These cannot be expressed as column tests, and they are the important ones."""
    tests = {p.stem for p in (DBT_DIR / "tests").glob("*.sql")}
    required = {
        "assert_subledger_ties_to_gl",
        "assert_inventory_value_equals_quantity_times_cost",
        "assert_as_of_join_does_not_multiply_facts",
    }
    assert required <= tests, f"missing singular tests: {required - tests}"
