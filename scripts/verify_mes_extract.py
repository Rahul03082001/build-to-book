#!/usr/bin/env python
"""Verify an MES extract independently of the generator that wrote it.

    scripts/verify_mes_extract.py data/
    scripts/verify_mes_extract.py /tmp/btb-full --json

Reads `mes_unit_events.csv` and `production_confirmations.csv` and recomputes
every invariant from the **files**, not from the generator's own tally. That
distinction is the point: the generator counts units as it emits them and
derives confirmations from that count, so asking it whether the two agree is
asking one object about itself. A real fault -- a truncated write, a chunk
dropped by the writer, a serial emitted twice -- lives in the file and not in
the tally.

This exists because the full profile's figures cannot be checked by the test
suite. Five million events take 45 seconds to generate and a minute to verify;
pytest asserts the same invariants on the demo profile instead
(tests/test_mes.py). Without this script the full-profile numbers quoted in
the README would rest on a run nobody could repeat.

Invariants checked, all of them exact:

* Every unit started completed or was scrapped. A unit that did neither has
  vanished and nobody could say where.
* Confirmations equal MES completions **per order**, not just in total.
  Offsetting errors cancel in a grand total, which is exactly the kind of
  agreement that looks like proof and is not.
* Every serial belongs to exactly one production order.
* No serial has two events for the same step and attempt.
* Per-step failure rates are within tolerance of the configured rates.

Exits 0 when every invariant holds, 1 when any fails, and 2 when the extract
is unreadable -- a missing file is a failure to verify, not a pass.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: How far a measured per-step failure rate may sit from the configured one.
#:
#: Sampling noise, not slack in the invariant: at the demo profile's ~15,000
#: units a 1% rate has a standard error around 0.08pp, so a tight bound would
#: fail on correct data roughly as often as it caught a fault. The exact
#: invariants above carry the weight; this one catches a rate wired to the
#: wrong step.
FAILURE_RATE_TOLERANCE_PCT = 0.5


class ExtractUnreadableError(Exception):
    """The extract could not be read, so nothing was verified."""


def _read_rows(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ExtractUnreadableError(f"{path} does not exist")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ExtractUnreadableError(f"{path} has a header but no rows")
    return rows


def verify(data_dir: pathlib.Path) -> dict[str, Any]:
    """Recompute every invariant from the written files.

    Returns a report with the measured figures and a list of violations. An
    empty violations list is the only pass.
    """
    from generator import config as genconfig

    config = genconfig.load()
    route = [step["name"] for step in config.domain["mes_steps"]]
    configured_rates = {step["name"]: step["failure_rate"] for step in config.domain["mes_steps"]}
    final_step = route[-1]

    events = _read_rows(data_dir / "mes_unit_events.csv")

    orders_by_serial: dict[str, set[str]] = collections.defaultdict(set)
    attempts_seen: collections.Counter[tuple[str, str, str]] = collections.Counter()
    # Highest attempt per (serial, step), so a reworked unit is judged on its
    # last attempt rather than its first.
    last_attempt: dict[tuple[str, str], tuple[int, str]] = {}
    first_attempt_results: dict[str, dict[str, str]] = collections.defaultdict(dict)
    step_attempts: collections.Counter[str] = collections.Counter()
    step_failures: collections.Counter[str] = collections.Counter()
    unknown_steps: set[str] = set()

    for row in events:
        serial, step = row["serial_number"], row["step_name"]
        attempt = int(row["attempt"])
        orders_by_serial[serial].add(row["order_number"])
        attempts_seen[(serial, step, row["attempt"])] += 1
        step_attempts[step] += 1
        if step not in configured_rates:
            unknown_steps.add(step)
        if row["result"] == "FAIL":
            step_failures[step] += 1
        if attempt == 1:
            first_attempt_results[serial][step] = row["result"]
        key = (serial, step)
        if key not in last_attempt or attempt >= last_attempt[key][0]:
            last_attempt[key] = (attempt, row["result"])

    completed_by_order: collections.Counter[str] = collections.Counter()
    scrapped_by_order: collections.Counter[str] = collections.Counter()
    for serial, orders in orders_by_serial.items():
        order = sorted(orders)[0]
        passed_final = last_attempt.get((serial, final_step), (0, "FAIL"))[1] == "PASS"
        if passed_final:
            completed_by_order[order] += 1
        else:
            scrapped_by_order[order] += 1

    units_started = len(orders_by_serial)
    units_completed = sum(completed_by_order.values())
    units_scrapped = sum(scrapped_by_order.values())
    first_pass = sum(
        1
        for results in first_attempt_results.values()
        if len(results) == len(route) and all(value == "PASS" for value in results.values())
    )

    confirmations = _read_rows(data_dir / "production_confirmations.csv")
    confirmed_by_order: collections.Counter[str] = collections.Counter()
    scrap_confirmed_by_order: collections.Counter[str] = collections.Counter()
    for row in confirmations:
        confirmed_by_order[row["order_number"]] += int(row["confirmed_quantity"])
        scrap_confirmed_by_order[row["order_number"]] += int(row["scrap_quantity"])

    violations: list[str] = []

    if units_completed + units_scrapped != units_started:
        violations.append(
            f"{units_started} units started but {units_completed} completed and "
            f"{units_scrapped} scrapped -- "
            f"{units_started - units_completed - units_scrapped} unaccounted for"
        )

    multi_order = [serial for serial, orders in orders_by_serial.items() if len(orders) > 1]
    if multi_order:
        violations.append(
            f"{len(multi_order)} serial(s) belong to more than one order, "
            f"e.g. {sorted(multi_order)[:3]}"
        )

    duplicates = [key for key, count in attempts_seen.items() if count > 1]
    if duplicates:
        violations.append(
            f"{len(duplicates)} (serial, step, attempt) combination(s) appear twice, "
            f"e.g. {sorted(duplicates)[:3]}"
        )

    if unknown_steps:
        violations.append(
            f"event(s) reference step(s) not in the configured route: {sorted(unknown_steps)}"
        )

    # Per order, because offsetting errors cancel in a grand total.
    disagreeing = sorted(
        order
        for order in set(completed_by_order) | set(confirmed_by_order)
        if completed_by_order[order] != confirmed_by_order[order]
        or scrapped_by_order[order] != scrap_confirmed_by_order[order]
    )
    if disagreeing:
        example = disagreeing[0]
        violations.append(
            f"{len(disagreeing)} order(s) disagree with their confirmations, "
            f"e.g. {example}: MES completed {completed_by_order[example]} / "
            f"scrapped {scrapped_by_order[example]}, ERP confirmed "
            f"{confirmed_by_order[example]} / scrapped {scrap_confirmed_by_order[example]}"
        )

    rates = {}
    for step in route:
        attempts = step_attempts[step]
        if not attempts:
            violations.append(f"step {step!r} is in the configured route but has no events")
            continue
        measured = 100.0 * step_failures[step] / attempts
        expected = 100.0 * configured_rates[step]
        rates[step] = {"attempts": attempts, "measured_pct": measured, "configured_pct": expected}
        if abs(measured - expected) > FAILURE_RATE_TOLERANCE_PCT:
            violations.append(
                f"step {step!r} failed {measured:.2f}% of attempts against a configured "
                f"{expected:.2f}% (tolerance {FAILURE_RATE_TOLERANCE_PCT}pp)"
            )

    return {
        "data_dir": str(data_dir),
        "events": len(events),
        "orders": len(confirmed_by_order),
        "units_started": units_started,
        "units_completed": units_completed,
        "units_scrapped": units_scrapped,
        "first_pass_yield_pct": 100.0 * first_pass / units_started if units_started else 0.0,
        "steps": rates,
        "violations": violations,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"MES extract: {report['data_dir']}")
    print(f"{'events':24}{report['events']:>12,}")
    print(f"{'orders':24}{report['orders']:>12,}")
    print(f"{'units started':24}{report['units_started']:>12,}")
    print(f"{'units completed':24}{report['units_completed']:>12,}")
    print(f"{'units scrapped':24}{report['units_scrapped']:>12,}")
    print(f"{'first-pass yield':24}{report['first_pass_yield_pct']:>11.1f}%")
    print()
    print(f"{'step':18}{'attempts':>12}{'measured':>11}{'configured':>12}")
    for step, figures in report["steps"].items():
        print(
            f"{step:18}{figures['attempts']:>12,}"
            f"{figures['measured_pct']:>10.2f}%{figures['configured_pct']:>11.2f}%"
        )
    print()
    if report["violations"]:
        print(f"FAILED -- {len(report['violations'])} invariant(s) violated:")
        for violation in report["violations"]:
            print(f"  - {violation}")
    else:
        print("Every invariant holds.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/verify_mes_extract.py",
        description="Verify an MES extract against its invariants, reading the written files.",
    )
    parser.add_argument(
        "data_dir",
        type=pathlib.Path,
        help="Directory holding mes_unit_events.csv and production_confirmations.csv.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = parser.parse_args(argv)

    try:
        report = verify(args.data_dir)
    except ExtractUnreadableError as error:
        # Exit 2, not 1: nothing was verified, which is a different outcome
        # from a verified failure and must not be mistaken for either.
        print(f"cannot verify: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
