"""The generator config and the defect registry must not drift apart.

The registry in `generator/defects.py` declares which control is expected to
catch each defect; `config.yaml` holds the switches. If a defect exists in one
and not the other, the Phase 4 detection test silently stops covering it --
which would be the worst kind of failure, because the table would still look
complete.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest

from generator import config as genconfig
from generator import defects

EXPECTED_DEFECT_COUNT = 13


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


def test_config_loads(cfg: genconfig.GeneratorConfig) -> None:
    assert cfg.seed > 0


def test_both_profiles_are_declared(cfg: genconfig.GeneratorConfig) -> None:
    """ADR-016: one small enough to host, one big enough to tune."""
    assert set(cfg.profiles) == {"demo", "full"}


def test_demo_profile_is_smaller_than_full(cfg: genconfig.GeneratorConfig) -> None:
    demo, full = cfg.profile("demo"), cfg.profile("full")
    for field in (
        "months",
        "materials",
        "suppliers",
        "purchase_order_lines",
        "production_orders",
        "mes_events",
    ):
        assert getattr(demo, field) < getattr(full, field), (
            f"demo.{field} is not smaller than full.{field}"
        )


def test_full_profile_meets_the_tuning_volume_target(cfg: genconfig.GeneratorConfig) -> None:
    """Below a few million events the tuning study has nothing to measure."""
    assert cfg.profile("full").mes_events >= 5_000_000


def test_unknown_profile_raises(cfg: genconfig.GeneratorConfig) -> None:
    with pytest.raises(KeyError):
        cfg.profile("staging")


def test_profile_period_start_is_a_date(cfg: genconfig.GeneratorConfig) -> None:
    assert isinstance(cfg.profile("demo").period_start, dt.date)


# ---------------------------------------------------------------------------
# Registry / config agreement
# ---------------------------------------------------------------------------
def test_thirteen_defects_are_declared(cfg: genconfig.GeneratorConfig) -> None:
    assert len(cfg.defects) == EXPECTED_DEFECT_COUNT
    assert len(defects.REGISTRY) == EXPECTED_DEFECT_COUNT


def test_registry_and_config_declare_the_same_defects(
    cfg: genconfig.GeneratorConfig,
) -> None:
    assert set(cfg.defects) == set(defects.REGISTRY), (
        "generator/config.yaml and generator/defects.py disagree about which defects exist"
    )


def test_registry_and_config_agree_on_phase(cfg: genconfig.GeneratorConfig) -> None:
    for key, spec in defects.REGISTRY.items():
        assert cfg.defects[key]["phase"] == spec.phase, (
            f"{key}: config says phase {cfg.defects[key]['phase']}, "
            f"registry says phase {spec.phase}"
        )


def test_every_defect_defaults_to_off(cfg: genconfig.GeneratorConfig) -> None:
    """Clean data is the default; a defect must be switched on deliberately."""
    on = cfg.enabled_defects()
    assert on == [], f"defects enabled by default: {on}"


def test_every_defect_describes_what_it_simulates(cfg: genconfig.GeneratorConfig) -> None:
    for key, spec in cfg.defects.items():
        assert spec.get("simulates", "").strip(), f"{key} has no `simulates` description"


def test_every_defect_names_a_control_expected_to_catch_it() -> None:
    for key, spec in defects.REGISTRY.items():
        assert spec.expected_control.startswith("CTRL-"), f"{key} does not name a control"


def test_the_two_phase_one_defects_are_the_expected_ones() -> None:
    """Phase 1 plants exactly these two, per the project specification."""
    phase_one = {k for k, s in defects.REGISTRY.items() if s.phase == 1}
    assert phase_one == {"duplicate_goods_receipt", "backdated_gl_posting"}


# ---------------------------------------------------------------------------
# Manifest contract
# ---------------------------------------------------------------------------
def test_manifest_rejects_an_unknown_defect_key() -> None:
    manifest = defects.DefectManifest(seed=1, profile="demo", generated_at=dt.datetime.now())
    with pytest.raises(KeyError):
        manifest.add(
            defects.InjectedRecord(
                defect_key="not_a_real_defect", extract="x", natural_key={"id": 1}
            )
        )


def test_manifest_counts_per_defect(tmp_path) -> None:
    manifest = defects.DefectManifest(
        seed=42, profile="demo", generated_at=dt.datetime(2026, 11, 2, 12, 0, 0)
    )
    for doc in ("4900000001", "4900000002"):
        manifest.add(
            defects.InjectedRecord(
                defect_key="duplicate_goods_receipt",
                extract="erp_goods_movements",
                natural_key={"material_document": doc, "item": 1},
            )
        )

    assert manifest.count_for("duplicate_goods_receipt") == 2
    assert manifest.count_for("backdated_gl_posting") == 0

    path = manifest.write(tmp_path / "defects_manifest.json")
    reloaded = defects.DefectManifest.read(path)
    assert reloaded["total_injected"] == 2
    assert reloaded["counts_by_defect"] == {"duplicate_goods_receipt": 2}
    assert reloaded["defects_enabled"] == ["duplicate_goods_receipt"]


def test_injector_lookup_names_the_phase_that_builds_it() -> None:
    """An unbuilt injector must say which phase builds it, not fail opaquely."""
    with pytest.raises(NotImplementedError, match="Phase 2"):
        defects.injector_for("uom_mismatch")


def test_register_rejects_an_unknown_defect_key() -> None:
    with pytest.raises(KeyError):
        defects.register("not_a_real_defect")


def test_freshness_window_declared_for_every_stale_checkable_source(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """CTRL-008 cannot check a source with no declared refresh window."""
    assert cfg.freshness_hours, "no freshness windows declared"
    for source, hours in cfg.freshness_hours.items():
        assert isinstance(hours, int) and hours > 0, f"{source} has an invalid window: {hours}"

    stale_target = cfg.defects["stale_extract"]["target"]
    assert stale_target in cfg.freshness_hours, (
        f"stale_extract targets {stale_target!r}, which has no freshness window"
    )


def test_the_supplier_late_delivery_rate_is_declared_and_plausible(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """Supplier on-time delivery is a Phase 3 KPI, so its assumption is config.

    Held here rather than in shipments.py so a reviewer can see what the
    generator assumes without reading the generator -- and so a value that
    would make the figure implausible fails at commit time. The first attempt
    put on-time delivery at 24%, which no manufacturer would recognise.
    """
    rate = cfg.domain["supplier_late_delivery_rate"]
    assert isinstance(rate, float), "the late-delivery rate must be a fraction, not a percentage"
    assert 0.0 < rate < 0.5, (
        f"a {rate:.0%} late rate means on-time delivery is {1 - rate:.0%}, which is "
        "not a supply chain worth reporting on"
    )


# ---------------------------------------------------------------------------
# scripts/explain_defect.py
#
# The failure-demo workflow branches on this script's exit code, and that
# workflow is workflow_dispatch-only -- it never runs in CI, so a mistake in it
# stays invisible until someone clicks the button. Which is exactly the fault
# the script was written to fix, so the contract it offers is pinned here.
# ---------------------------------------------------------------------------
def _explain_defect() -> Any:
    import importlib.util

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "explain_defect.py"
    spec = importlib.util.spec_from_file_location("explain_defect", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_implemented_defect_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    module = _explain_defect()
    assert module.main(["duplicate_goods_receipt"]) == module.EXIT_READY
    out = capsys.readouterr().out
    assert "CTRL-001" in out, "the expected control must be named"
    assert "--defect duplicate_goods_receipt" in out, "it should say how to plant it"


def test_an_unbuilt_injector_exits_three_and_says_which_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 3 is what the workflow reads to say TWO things are missing, not one.

    A reader who picks a Phase 2 defect today is hitting both an unbuilt
    injector and an unbuilt demo. Collapsing those into one `exit 1` is what
    made the old stub unreadable.
    """
    module = _explain_defect()
    assert module.main(["missing_supplier_on_shipment"]) == module.EXIT_NOT_IMPLEMENTED
    out = capsys.readouterr().out
    assert "Phase 2" in out, "the phase that builds the injector must be named"
    assert "not a mistake" in out, (
        "choosing a listed-but-unbuilt defect is a reasonable thing to do and "
        "the output should say so rather than implying user error"
    )
    # And it must name what *can* be run instead.
    assert "duplicate_goods_receipt" in out


def test_an_unknown_defect_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """Distinct from exit 3: the workflow fails fast on this one.

    A key that is not in the registry means the dropdown and the registry have
    drifted apart, which is a repo fault rather than a phase boundary.
    """
    module = _explain_defect()
    assert module.main(["no_such_defect"]) == module.EXIT_UNKNOWN
    out = capsys.readouterr().out
    assert "not a defect in the registry" in out
    assert "duplicate_goods_receipt" in out, "it should list what is known"


def test_the_three_exit_codes_are_distinct() -> None:
    """The workflow branches on them, so collapsing two would merge two cases."""
    module = _explain_defect()
    codes = {module.EXIT_READY, module.EXIT_UNKNOWN, module.EXIT_NOT_IMPLEMENTED}
    assert len(codes) == 3, f"exit codes collide: {codes}"
    assert module.EXIT_READY == 0, "success must be zero"


def test_listing_every_defect_reports_each_injector_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from generator import defects

    module = _explain_defect()
    assert module.main(["--list"]) == module.EXIT_READY
    out = capsys.readouterr().out
    for key in defects.REGISTRY:
        assert key in out, f"{key} is missing from the listing"
    assert f"{len(defects.INJECTORS)} of {len(defects.REGISTRY)} injectors built" in out


def test_every_dropdown_option_is_a_registered_defect() -> None:
    """The workflow's choice list and the registry must not drift.

    An option that is not in the registry would exit 2 and fail the run with
    "not a defect in the registry" -- a repo fault presented to whoever
    clicked, after a four-minute dependency install.
    """
    import yaml

    from generator import defects

    workflow = yaml.safe_load(
        (
            pathlib.Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "demo_failure.yml"
        ).read_text()
    )
    triggers = workflow.get(True, workflow.get("on"))
    options = triggers["workflow_dispatch"]["inputs"]["defect"]["options"]

    unknown = sorted(set(options) - set(defects.REGISTRY))
    assert not unknown, f"dropdown offers defect(s) not in the registry: {unknown}"
    missing = sorted(set(defects.REGISTRY) - set(options))
    assert not missing, f"registry defect(s) the dropdown does not offer: {missing}"

    default = triggers["workflow_dispatch"]["inputs"]["defect"]["default"]
    assert default in defects.INJECTORS, (
        f"the default option {default!r} has no injector, so the button's "
        "out-of-the-box behaviour is the least informative path"
    )


def test_the_command_it_prints_plants_only_the_chosen_defect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The printed `make` command must do what the text claims.

    `make data-defects` takes no DEFECTS variable -- it passes `--defect all`
    and plants every implemented defect. The first version of this script
    printed `make data-defects DEFECTS=<key>`, which would have quietly planted
    more than the reader asked for while appearing to name one. Found by
    reading the target rather than by any test, which is why this exists.
    """
    module = _explain_defect()
    module.main(["duplicate_goods_receipt"])
    printed = capsys.readouterr().out

    assert "make data DEFECTS=duplicate_goods_receipt" in printed
    assert "data-defects" not in printed, (
        "data-defects ignores DEFECTS and plants every defect, so naming it "
        "beside a single key misrepresents what the command does"
    )

    makefile = (pathlib.Path(__file__).resolve().parent.parent / "Makefile").read_text()
    # The `data` target must actually forward DEFECTS, or the command above is
    # wrong in the other direction.
    data_target = makefile.split("\ndata:", 1)[1].split("\n.PHONY", 1)[0]
    assert "DEFECTS" in data_target, "make data no longer forwards DEFECTS"


def test_the_demo_profile_implies_a_plausible_business(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """The declared volumes must describe a company that could exist.

    `mes_events` sets the order quantity, which sets how many racks are built,
    which sets how much material is bought. Those four numbers were never
    checked against each other for plausibility, and at 60,000 declared events
    the demo profile implied 14,800 racks a quarter -- about $60B a year of
    component purchasing, which is four times Supermicro's revenue. The figure
    only became visible when ADR-027 made purchasing cover consumption; before
    that, production volume had no monetary consequence at all.

    The band is wide on purpose. This is not a test of a target number, it is a
    guard against a profile that silently stops describing a manufacturer --
    the specific failure being that nothing connected the MES event count to
    the money.
    """
    from generator import mes

    profile = cfg.profile("demo")
    steps = cfg.domain["mes_steps"]
    units_per_order = mes.order_quantity_for_profile(
        declared_events=profile.mes_events,
        order_count=profile.production_orders,
        step_count=len(steps),
        max_rework_attempts=int(cfg.domain["max_rework_attempts"]),
        step_failure_rates=[float(s.get("failure_rate", 0.0)) for s in steps],
    )
    racks_per_window = units_per_order * profile.production_orders
    windows_per_year = 12 / profile.months
    racks_per_year = racks_per_window * windows_per_year

    # Measured from the generated data: the standard cost of the components in
    # one rack. Stated here rather than recomputed, because the point is the
    # order of magnitude and a drifting rack cost should move this test's
    # message rather than quietly keep it passing.
    rack_component_cost = 1_013_063
    implied_annual = racks_per_year * rack_component_cost

    assert 1e9 <= implied_annual <= 2e10, (
        f"the demo profile implies {racks_per_year:,.0f} racks a year at "
        f"${rack_component_cost:,} each -- ${implied_annual / 1e9:,.1f}B of annual "
        "component purchasing, which is not a manufacturer anyone would recognise. "
        "mes_events drives the order quantity, which drives the money."
    )


def test_the_full_profile_is_a_volume_profile_not_a_business(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """And the full profile is deliberately NOT plausible, which must be said.

    5,000,000 MES events divided by roughly four events per unit is about 1.2
    million racks, and at a million dollars a rack that is over a trillion
    dollars a quarter. No arrangement of orders changes that: the event count
    and the unit cost are what they are.

    The profile exists to prove the pipeline handles five million rows (ADR-016,
    ADR-024), not to describe a company. This test exists so that fact is
    asserted rather than assumed, and so nobody quotes a monetary total from a
    `full` run as a business figure.
    """
    profile = cfg.profile("full")
    assert profile.mes_events >= 5_000_000
    assert "performance" in profile.description.lower() or "tuning" in profile.description.lower()
    assert "not" in profile.description.lower(), (
        "the full profile's description must say what it is not, or its "
        "monetary totals will be read as business facts"
    )
