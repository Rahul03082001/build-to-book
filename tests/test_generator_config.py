"""The generator config and the defect registry must not drift apart.

The registry in `generator/defects.py` declares which control is expected to
catch each defect; `config.yaml` holds the switches. If a defect exists in one
and not the other, the Phase 4 detection test silently stops covering it --
which would be the worst kind of failure, because the table would still look
complete.
"""

from __future__ import annotations

import datetime as dt

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
