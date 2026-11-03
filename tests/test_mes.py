"""Unit-level MES events (BTB-33).

Three things matter here beyond the data being plausible.

**Volume.** 5,000,000 events cannot be held in memory, so generation streams
and a test asserts peak memory does not grow with row count. Measured: 289 MB
peak for 4,990,452 events, against the ~4.3 GB that holding them as dicts
would have cost.

**The two config numbers must agree.** `config.yaml` declares both
`production_orders` and `mes_events`. Order quantities used to be
`randint(1, 9)`, which produced ~158,000 events against a declared 5,000,000 --
a factor of 32, and nothing checked it.

**Units must be accounted for.** Started = completed + scrapped. A unit in
neither has vanished, and confirmations are derived from those counts so
CTRL-005 reconciles exactly rather than approximately.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as dt
import pathlib
import re
import resource
import subprocess
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

import tests.conftest as conftest
from generator import config as genconfig
from generator import dataset, mes, writers


def _load_verifier() -> Any:
    """Load scripts/verify_mes_extract.py by path.

    `scripts/` is deliberately not a package: pyproject lists the four modules
    that ship in the distribution, and a CLI used by `make verify-mes` and CI
    is not one of them. Importing by path keeps the script a script while
    letting its invariants be tested.
    """
    import importlib.util

    path = conftest.REPO_ROOT / "scripts" / "verify_mes_extract.py"
    spec = importlib.util.spec_from_file_location("verify_mes_extract", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_mes_extract = _load_verifier()


@pytest.fixture(scope="module")
def cfg() -> genconfig.GeneratorConfig:
    return genconfig.load()


@pytest.fixture(scope="module")
def streamed(cfg: genconfig.GeneratorConfig) -> dict[str, Any]:
    """Generate the demo profile and consume the MES stream once."""
    data, _ = dataset.build(cfg, "demo")
    events: list[dict[str, Any]] = []
    chunk_sizes: list[int] = []
    for chunk in data.streams["mes_unit_events"]():
        chunk_sizes.append(len(chunk))
        events.extend(chunk)
    return {
        "data": data,
        "events": events,
        "chunk_sizes": chunk_sizes,
        "outcome": data.mes_outcome,
        "confirmations": data.deferred["production_confirmations"](),
    }


# ---------------------------------------------------------------------------
# The grain
# ---------------------------------------------------------------------------
def test_grain_is_one_row_per_serial_per_step_attempt(streamed: dict[str, Any]) -> None:
    keys = [(e["serial_number"], e["step_sequence"], e["attempt"]) for e in streamed["events"]]
    duplicates = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not duplicates, f"duplicate event keys: {duplicates[:3]}"


def test_every_serial_belongs_to_a_production_order(streamed: dict[str, Any]) -> None:
    """Without this, MES and ERP cannot be reconciled at all (CTRL-005)."""
    orders = set(streamed["data"].order_plan)
    for event in streamed["events"]:
        assert event["order_number"] in orders, event["serial_number"]


def test_serials_walk_the_route_in_order(streamed: dict[str, Any]) -> None:
    by_serial: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in streamed["events"]:
        by_serial[event["serial_number"]].append(event)

    for serial, events in by_serial.items():
        sequences = [e["step_sequence"] for e in events]
        assert sequences == sorted(sequences), f"{serial} events are out of order"
        # Step sequences must be contiguous from 1: a unit cannot skip a step.
        distinct = sorted(set(sequences))
        assert distinct == list(range(1, len(distinct) + 1)), serial


def test_a_scrapped_unit_does_not_reach_later_steps(streamed: dict[str, Any]) -> None:
    """A unit out of rework attempts is scrapped there.

    It cannot pass a step it never reached, and letting it continue would
    inflate first-pass yield at the later steps.
    """
    by_serial: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in streamed["events"]:
        by_serial[event["serial_number"]].append(event)

    total_steps = len(genconfig.load().domain["mes_steps"])
    for serial, events in by_serial.items():
        ordered = sorted(events, key=lambda e: (e["step_sequence"], e["attempt"]))
        last = ordered[-1]
        if last["result"] == "FAIL":
            # Scrapped: it stopped at this step, so it saw fewer than all steps.
            assert last["step_sequence"] <= total_steps
            assert {e["step_sequence"] for e in ordered} == set(
                range(1, last["step_sequence"] + 1)
            ), f"{serial} has events after the step it was scrapped at"


def test_rework_is_bounded(streamed: dict[str, Any], cfg: genconfig.GeneratorConfig) -> None:
    limit = int(cfg.domain["max_rework_attempts"]) + 1
    attempts = {e["attempt"] for e in streamed["events"]}
    assert max(attempts) <= limit, f"attempt {max(attempts)} exceeds the limit {limit}"
    assert max(attempts) > 1, "no rework occurred, so the loop is untested"


def test_failures_carry_a_code_and_passes_do_not(streamed: dict[str, Any]) -> None:
    for event in streamed["events"]:
        if event["result"] == "FAIL":
            assert event["failure_code"], event
        else:
            assert event["failure_code"] is None, event


def test_every_failure_code_is_declared(
    streamed: dict[str, Any], cfg: genconfig.GeneratorConfig
) -> None:
    declared = set(cfg.domain["failure_codes"])
    used = {e["failure_code"] for e in streamed["events"] if e["failure_code"]}
    assert used <= declared
    assert used == declared, (
        f"only {len(used)} of {len(declared)} failure codes appear; the rest are untested"
    )


def test_per_step_failure_rates_match_the_configuration(
    streamed: dict[str, Any], cfg: genconfig.GeneratorConfig
) -> None:
    """Within sampling tolerance. A step failing at the wrong rate would make
    first-pass yield -- the headline Phase 3 KPI -- quietly wrong."""
    totals: collections.Counter[str] = collections.Counter()
    failures: collections.Counter[str] = collections.Counter()
    for event in streamed["events"]:
        totals[event["step_name"]] += 1
        if event["result"] == "FAIL":
            failures[event["step_name"]] += 1

    for step in cfg.domain["mes_steps"]:
        name = step["name"]
        expected = float(step["failure_rate"])
        observed = failures[name] / totals[name]
        assert abs(observed - expected) < max(0.01, expected * 0.3), (
            f"{name}: observed {observed:.3%}, configured {expected:.3%}"
        )


def test_first_pass_yield_is_realistic(streamed: dict[str, Any]) -> None:
    """The headline Phase 3 KPI.

    A yield of 100% would make the metric meaningless, and a very low one would
    not resemble a manufacturer that ships.
    """
    by_serial: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for event in streamed["events"]:
        by_serial[event["serial_number"]].append(event)
    clean = sum(1 for events in by_serial.values() if all(e["result"] == "PASS" for e in events))
    yield_rate = clean / len(by_serial)
    assert 0.80 < yield_rate < 0.97, f"first-pass yield is {yield_rate:.1%}"


# ---------------------------------------------------------------------------
# Units accounted for, and the tie to ERP
# ---------------------------------------------------------------------------
def test_every_unit_started_is_completed_or_scrapped(streamed: dict[str, Any]) -> None:
    outcome = streamed["outcome"]
    assert outcome.units_started > 0
    assert outcome.accounted_for(), (
        f"{outcome.units_started} started but "
        f"{outcome.units_completed} completed + {outcome.units_scrapped} scrapped"
    )


def test_confirmations_derive_from_the_mes_outcome(streamed: dict[str, Any]) -> None:
    """CTRL-005's clean baseline must be exact, not approximate.

    Generating the two sides independently would leave a gap made of noise, and
    a control whose baseline is noise cannot detect a real finding.
    """
    outcome = streamed["outcome"]
    confirmed = sum(c["confirmed_quantity"] for c in streamed["confirmations"])
    scrapped = sum(c["scrap_quantity"] for c in streamed["confirmations"])
    assert confirmed == outcome.units_completed
    assert scrapped == outcome.units_scrapped


def test_confirmations_exist_for_every_order_with_activity(
    streamed: dict[str, Any],
) -> None:
    confirmed_orders = {c["order_number"] for c in streamed["confirmations"]}
    active = {e["order_number"] for e in streamed["events"]}
    assert active <= confirmed_orders, (
        f"{len(active - confirmed_orders)} orders had MES activity but no confirmation"
    )


# ---------------------------------------------------------------------------
# Volume: the reason ADR-024 exists
# ---------------------------------------------------------------------------
def test_order_quantity_is_derived_from_the_declared_event_count(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """The two config numbers must agree by construction.

    They did not: order quantities were randint(1, 9), producing ~158,000
    events against a declared 5,000,000 on the full profile.
    """
    rates = [float(s["failure_rate"]) for s in cfg.domain["mes_steps"]]
    steps = len(cfg.domain["mes_steps"])
    rework = int(cfg.domain["max_rework_attempts"])

    for name in ("demo", "full"):
        profile = cfg.profile(name)
        quantity = mes.order_quantity_for_profile(
            profile.mes_events, profile.production_orders, steps, rework, rates
        )
        expected_attempts = 0.0
        for rate in rates:
            attempts, probability = 1.0, rate
            for _ in range(rework):
                attempts += probability
                probability *= rate
            expected_attempts += attempts
        predicted = profile.production_orders * quantity * expected_attempts
        ratio = predicted / profile.mes_events
        assert 0.9 < ratio < 1.1, (
            f"{name}: derived quantity {quantity} predicts {predicted:,.0f} events "
            f"against a declared {profile.mes_events:,} ({ratio:.1%})"
        )


def test_demo_event_count_is_close_to_the_declared_target(
    streamed: dict[str, Any], cfg: genconfig.GeneratorConfig
) -> None:
    declared = cfg.profile("demo").mes_events
    actual = len(streamed["events"])
    assert 0.9 < actual / declared < 1.1, (
        f"generated {actual:,} events against a declared {declared:,}"
    )


def test_generation_streams_in_chunks(streamed: dict[str, Any]) -> None:
    """A single chunk would mean the whole extract is in memory after all."""
    sizes = streamed["chunk_sizes"]
    assert len(sizes) > 1, "MES was yielded as one chunk, so it is not streaming"
    # A chunk may overshoot by at most one unit's events, because a serial's
    # route is deliberately never split across chunks.
    cfg = genconfig.load()
    overshoot = mes.max_events_per_unit(
        len(cfg.domain["mes_steps"]), int(cfg.domain["max_rework_attempts"])
    )
    assert max(sizes) <= mes.CHUNK_SIZE + overshoot, (
        f"a chunk of {max(sizes)} exceeds {mes.CHUNK_SIZE} by more than one unit"
    )


def test_the_stream_can_be_consumed_more_than_once(
    cfg: genconfig.GeneratorConfig,
) -> None:
    """Held as a factory, not an iterator.

    A spent generator would silently write an empty file on a second pass --
    and the control totals would agree with it, because they are computed from
    what was written.
    """
    data, _ = dataset.build(cfg, "demo")
    first = sum(len(c) for c in data.streams["mes_unit_events"]())
    second = sum(len(c) for c in data.streams["mes_unit_events"]())
    assert first == second > 0


def test_streaming_mes_does_not_grow_memory_with_row_count(
    tmp_path: pathlib.Path, cfg: genconfig.GeneratorConfig
) -> None:
    """ADR-024's reason for existing, on the real generator rather than a probe.

    5,000,000 events held as dicts measured ~4.3 GB; streamed, the full profile
    peaks at 289 MB. This checks the demo profile does not balloon, which is
    the property that makes the full one possible.
    """
    data, _ = dataset.build(cfg, "demo")
    spec = dataset.EXTRACT_SPECS["mes_unit_events"]

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = writers.write_extract_streaming(
        spec, data.streams["mes_unit_events"](), tmp_path, dt.datetime(2026, 11, 2)
    )
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert result.row_count > 10_000, "too few rows for this to mean anything"
    growth = (after - before) / max(before, 1)
    assert growth < 0.3, f"peak RSS grew {growth:.0%} writing {result.row_count:,} rows"


def test_mes_is_declared_streamed_and_confirmations_deferred() -> None:
    """The ordering the deferred extract depends on.

    LOAD_ORDER must place mes_unit_events before production_confirmations:
    reversing it would silently produce empty confirmations, and the control
    totals would agree with them.
    """
    assert "mes_unit_events" in dataset.STREAMED
    assert "production_confirmations" in dataset.DEFERRED
    order = list(dataset.LOAD_ORDER)
    assert order.index("mes_unit_events") < order.index("production_confirmations")


def test_streamed_extracts_are_not_also_in_memory(
    streamed: dict[str, Any],
) -> None:
    """Holding it both ways would defeat the point."""
    assert "mes_unit_events" not in streamed["data"].as_mapping()
    assert "production_confirmations" not in streamed["data"].as_mapping()


# ---------------------------------------------------------------------------
# CTRL-005 against a built warehouse
#
# The tests above prove the generator's two sides agree. These prove the
# control notices when they do not -- which the clean run cannot show, because
# on a clean run it passes no matter how it is written. A control that has
# never failed in a test is an assertion about the generator, not a control.
#
# Each case mutates `raw`, rebuilds into a sandbox schema, and restores. The
# sandbox prefix keeps the live marts untouched while the mutation is in place
# (ADR-011's reason for the prefix existing).
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def clean_batch(warehouse: psycopg.Connection) -> str:
    """A clean load and build this module put there itself."""
    return conftest.establish_clean_warehouse(warehouse)


def _ctrl_005(conn: psycopg.Connection, batch_id: str, schema_prefix: str) -> list[Any]:
    from controls import checks, runner

    summary = runner.run_checks(
        conn,
        schema_prefix=schema_prefix,
        batch_id=batch_id,
        checks=(checks.MES_TO_ERP_COMPLETION,),
    )
    return summary.results


def _rebuild_sandbox() -> None:
    result = subprocess.run(
        [
            str(conftest.REPO_ROOT / ".venv" / "bin" / "dbt"),
            "build",
            "--project-dir",
            str(conftest.REPO_ROOT / "warehouse" / "dbt"),
            "--profiles-dir",
            str(conftest.REPO_ROOT / "warehouse" / "dbt"),
            "--vars",
            "{schema_prefix: sandbox_}",
        ],
        capture_output=True,
        text=True,
        cwd=conftest.REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"sandbox dbt build unavailable: {result.stdout[-400:]}")


@contextlib.contextmanager
def _mutated_raw(conn: psycopg.Connection, sql: str, params: dict[str, Any]) -> Iterator[None]:
    """Apply a mutation to raw, rebuild the sandbox, and always restore.

    The restore runs in a `finally` so a failing assertion cannot leave the
    warehouse holding a planted defect. A later module taking "whatever ran
    last" as its input has already caused one false failure in this suite.
    """
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "create temp table _ctrl005_backup as "
            "select * from raw.production_confirmations where order_number = %(order_number)s",
            params,
        )
        cursor.execute(sql, params)
    try:
        _rebuild_sandbox()
        yield
    finally:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                "delete from raw.production_confirmations where order_number = %(order_number)s",
                params,
            )
            cursor.execute("insert into raw.production_confirmations select * from _ctrl005_backup")
            cursor.execute("drop table _ctrl005_backup")


def _an_order_with_completions(conn: psycopg.Connection) -> str:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "select order_number from intermediate.int_mes_to_erp_completion "
            "where mes_units_completed > 0 order by order_number limit 1"
        )
        row = cursor.fetchone()
    assert row is not None, "no order has MES completions, so there is nothing to reconcile"
    return str(row[0])


def test_ctrl_005_passes_and_every_order_reconciles(
    warehouse: psycopg.Connection, clean_batch: str
) -> None:
    """The clean baseline must be exact: every order RECONCILED, not just a
    total that happens to agree."""
    results = _ctrl_005(warehouse, clean_batch, "")
    assert results and all(r.passed for r in results), [r.detail for r in results]

    with warehouse.transaction(), warehouse.cursor() as cursor:
        cursor.execute(
            "select completion_status, count(*) from intermediate.int_mes_to_erp_completion "
            "group by completion_status"
        )
        by_status = dict(cursor.fetchall())
    assert set(by_status) == {"RECONCILED"}, f"not every order reconciles: {by_status}"
    assert by_status["RECONCILED"] > 0


def test_ctrl_005_catches_a_missing_confirmation(
    warehouse: psycopg.Connection, clean_batch: str
) -> None:
    """Units built and never reported to finance -- the serious direction.

    The reconciliation left-joins confirmations precisely so this case appears;
    an inner join would drop the order and the control would pass.
    """
    order = _an_order_with_completions(warehouse)
    with _mutated_raw(
        warehouse,
        "delete from raw.production_confirmations where order_number = %(order_number)s",
        {"order_number": order},
    ):
        results = _ctrl_005(warehouse, clean_batch, "sandbox_")

    assert any(not r.passed for r in results), "a vanished confirmation did not trip CTRL-005"
    detail = " ".join(r.detail or "" for r in results)
    assert order in detail, f"CTRL-005 failed but did not name order {order}: {detail}"
    assert "MES_WITHOUT_CONFIRMATION" in detail


def test_ctrl_005_catches_a_single_unit_gap(
    warehouse: psycopg.Connection, clean_batch: str
) -> None:
    """One unit out of more than a million is still a finding.

    Tolerance is zero (ADR-010): both sides are integer unit counts, so any
    difference is real rather than rounding.
    """
    order = _an_order_with_completions(warehouse)
    with _mutated_raw(
        warehouse,
        "update raw.production_confirmations set confirmed_quantity = confirmed_quantity - 1 "
        "where order_number = %(order_number)s",
        {"order_number": order},
    ):
        results = _ctrl_005(warehouse, clean_batch, "sandbox_")

    assert any(not r.passed for r in results), "a one-unit gap did not trip CTRL-005"
    detail = " ".join(r.detail or "" for r in results)
    assert order in detail
    assert "COMPLETION_GAP" in detail
    assert "gap 1" in detail, f"the gap should be reported as 1: {detail}"


def test_the_live_marts_survive_the_sandbox_mutations(
    warehouse: psycopg.Connection, clean_batch: str
) -> None:
    """Runs last, and asserts the restore actually restored.

    Ordered after the mutation tests on purpose: if a `finally` failed to put
    raw back, this is what says so, rather than an unrelated module failing
    later for reasons that take an hour to trace.
    """
    results = _ctrl_005(warehouse, clean_batch, "")
    assert all(r.passed for r in results), (
        f"the live build did not survive the mutations: {[r.detail for r in results]}"
    )


# ---------------------------------------------------------------------------
# The standalone verifier
#
# scripts/verify_mes_extract.py recomputes the invariants from the written
# files, which is how the full profile's figures are checked -- five million
# events are too slow for the suite. These tests run it on the demo profile and,
# more importantly, prove it fails when the extract is wrong. A verifier that
# has only ever passed is a script that prints numbers.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def demo_extract(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    out = tmp_path_factory.mktemp("mes-extract")
    conftest.generate_extracts(out)
    return out


def test_the_verifier_passes_a_clean_extract(demo_extract: pathlib.Path) -> None:
    report = verify_mes_extract.verify(demo_extract)
    assert report["violations"] == []
    assert report["units_started"] > 0
    assert report["units_completed"] + report["units_scrapped"] == report["units_started"]


def test_the_verifier_exits_zero_on_a_clean_extract(demo_extract: pathlib.Path) -> None:
    """The exit code is what `make verify-mes` and CI act on."""
    assert verify_mes_extract.main([str(demo_extract), "--json"]) == 0


def _rewrite(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _rows(path: pathlib.Path) -> list[dict[str, str]]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_the_verifier_catches_offsetting_confirmation_errors(
    demo_extract: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The case a total-only check cannot see.

    One order gains a unit and another loses one, so the grand total still
    agrees exactly. This is why the comparison is per order: an aggregate that
    reconciles is not evidence that its parts do.
    """
    import shutil

    work = tmp_path / "offsetting"
    shutil.copytree(demo_extract, work)
    confirmations = work / "production_confirmations.csv"
    rows = _rows(confirmations)
    rows[0]["confirmed_quantity"] = str(int(rows[0]["confirmed_quantity"]) + 1)
    rows[1]["confirmed_quantity"] = str(int(rows[1]["confirmed_quantity"]) - 1)
    _rewrite(confirmations, rows)

    before = sum(
        int(r["confirmed_quantity"]) for r in _rows(demo_extract / "production_confirmations.csv")
    )
    after = sum(int(r["confirmed_quantity"]) for r in rows)
    assert before == after, "the mutation must leave the grand total unchanged to be meaningful"

    report = verify_mes_extract.verify(work)
    assert report["violations"], "offsetting per-order errors were not detected"
    assert "disagree with their confirmations" in " ".join(report["violations"])


def test_the_verifier_catches_a_truncated_write(
    demo_extract: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A dropped final_test row makes a completed unit look scrapped."""
    import shutil

    work = tmp_path / "truncated"
    shutil.copytree(demo_extract, work)
    events = work / "mes_unit_events.csv"
    rows = _rows(events)
    serial = rows[-1]["serial_number"]
    kept = [
        r for r in rows if not (r["serial_number"] == serial and r["step_name"] == "final_test")
    ]
    assert len(kept) < len(rows), "the mutation removed nothing"
    _rewrite(events, kept)

    report = verify_mes_extract.verify(work)
    assert report["violations"], "a dropped final_test row was not detected"


def test_the_verifier_catches_a_duplicated_event(
    demo_extract: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    import shutil

    work = tmp_path / "duplicated"
    shutil.copytree(demo_extract, work)
    events = work / "mes_unit_events.csv"
    rows = _rows(events)
    _rewrite(events, [*rows, dict(rows[10])])

    report = verify_mes_extract.verify(work)
    assert any("appear twice" in v for v in report["violations"]), report["violations"]


def test_an_unreadable_extract_is_not_a_pass(tmp_path: pathlib.Path) -> None:
    """Exit 2, not 0.

    A missing file means nothing was verified, which must not be reported as
    "every invariant holds" -- that is how an empty run becomes evidence.
    """
    assert verify_mes_extract.main([str(tmp_path)]) == 2
    with pytest.raises(verify_mes_extract.ExtractUnreadableError):
        verify_mes_extract.verify(tmp_path)


def test_the_make_target_and_the_script_agree_on_the_default_directory(
    repo_root: pathlib.Path,
) -> None:
    """`make verify-mes` with no arguments must point at where `make data` wrote.

    If the two drifted, the target would verify a stale extract, or none, and
    report a pass either way for the second reason.
    """
    import yaml

    makefile = (repo_root / "Makefile").read_text()
    match = re.search(r"^EXTRACT_DIR\s*\?=\s*(\S+)", makefile, re.MULTILINE)
    assert match, "the Makefile declares no EXTRACT_DIR"

    config = yaml.safe_load((repo_root / "generator" / "config.yaml").read_text())
    assert match.group(1) == config["output"]["directory"]
