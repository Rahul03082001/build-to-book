"""Unit-level MES events (BTB-33).

One row per serial number per step attempt. A rack walks the route in order --
assembly, firmware flash, burn-in, final test -- and a failure sends it back
for rework up to the configured limit, after which it is scrapped.

This is the first extract where volume is the design constraint: the `full`
profile calls for 5,000,000 events, which measured at ~929 bytes per row as
Python dicts would be about 4.3 GB. So generation **yields chunks** and the
writer streams them (ADR-024). Peak memory is a function of chunk size rather
than row count.

Two invariants matter downstream:

* **Every serial belongs to a production order**, so MES and ERP can be
  reconciled at all (CTRL-005).
* **Units started = completed + scrapped.** A unit that neither passes final
  test nor gets scrapped has vanished, and nobody could say where. The
  generator counts both so production confirmations can be derived from the
  MES outcome rather than invented alongside it.

Order quantities are derived from the profile's declared event count rather
than drawn from a fixed range. They used to be `randint(1, 9)`, which produced
about 158,000 events against a declared 5,000,000 -- the two numbers in
config.yaml disagreed by a factor of 32 and nothing noticed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator, Sequence
from typing import Any

from generator.writers import ExtractSpec

#: Rows yielded per chunk. Large enough that per-chunk overhead is noise,
#: small enough that peak memory stays flat -- measured at ~91 MB from 100k to
#: 5M rows.
#:
#: A chunk may overshoot this slightly: a unit's events are kept together so a
#: serial's route is never split across chunks, which keeps the stream
#: readable and makes a partial write obvious rather than subtle. The overshoot
#: is bounded by the events one unit can generate.
CHUNK_SIZE = 50_000

#: Hours a unit waits between steps. Burn-in dominates the route, so the gaps
#: elsewhere are short.
INTER_STEP_HOURS = (1, 6)

EXTRACT_SPECS: dict[str, ExtractSpec] = {
    "mes_unit_events": ExtractSpec(
        name="mes_unit_events",
        columns=[
            "serial_number",
            "order_number",
            "material_number",
            "step_name",
            "step_sequence",
            "attempt",
            "station",
            "started_at",
            "finished_at",
            "result",
            "failure_code",
            "is_final_attempt",
            "operator",
        ],
        source_system="MES",
        # Events carry no money. Completeness rests on the row count, which is
        # why the trailer's row count is the primary check for this extract.
        value_column=None,
        natural_key=["serial_number", "step_sequence", "attempt"],
    ),
}

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"


@dataclasses.dataclass
class MesOutcome:
    """What MES recorded, per production order.

    Production confirmations are generated from this rather than independently,
    so CTRL-005 reconciles exactly on a clean run. Generating both sides
    separately would leave a gap made of noise, and a control whose baseline is
    noise cannot detect a real finding.
    """

    event_count: int = 0
    units_started: int = 0
    completed_by_order: dict[str, int] = dataclasses.field(default_factory=dict)
    scrapped_by_order: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def units_completed(self) -> int:
        return sum(self.completed_by_order.values())

    @property
    def units_scrapped(self) -> int:
        return sum(self.scrapped_by_order.values())

    def accounted_for(self) -> bool:
        """Every unit started must have completed or been scrapped."""
        return self.units_started == self.units_completed + self.units_scrapped

    def reset(self) -> None:
        """Clear the tallies in place, ready for another pass.

        In place, not by rebinding a fresh instance: the dataset hands this
        object to the confirmations writer when it composes the run, before
        anything has streamed. Rebinding left that writer holding an outcome
        that stayed at zero while the real tallies accumulated elsewhere --
        confirmations of 14,793 against a reported 0 units started.
        """
        self.event_count = 0
        self.units_started = 0
        self.completed_by_order.clear()
        self.scrapped_by_order.clear()


def max_events_per_unit(step_count: int, max_rework_attempts: int) -> int:
    """Most events one unit can generate: every step, every permitted attempt.

    This is the bound on how far a chunk may overshoot `CHUNK_SIZE`, because a
    unit's events are never split across chunks. Tests assert the overshoot
    stays within it rather than asserting chunks are exactly `CHUNK_SIZE`,
    which would be a claim the generator deliberately does not make.
    """
    return step_count * (max_rework_attempts + 1)


def order_quantity_for_profile(
    declared_events: int,
    order_count: int,
    step_count: int,
    max_rework_attempts: int,
    step_failure_rates: Sequence[float],
) -> int:
    """Units per order implied by the profile's declared event count.

    `config.yaml` declares both `production_orders` and `mes_events`, and the
    two have to agree. They did not: `target_quantity` was `randint(1, 9)`,
    which produced ~158,000 events against a declared 5,000,000 on the full
    profile -- a factor of 32, and nothing checked it.

    Expected attempts per unit per step is slightly above one, because a
    failure is retried. Summing the per-step expectations gives the events a
    single unit generates, and the declared total divided by that gives the
    units needed.
    """
    if order_count <= 0 or step_count <= 0:
        return 1

    expected_attempts = 0.0
    for rate in step_failure_rates:
        # attempts = 1 + p + p^2 + ... up to the rework limit.
        attempts = 1.0
        probability = rate
        for _ in range(max_rework_attempts):
            attempts += probability
            probability *= rate
        expected_attempts += attempts

    events_per_unit = expected_attempts or float(step_count)
    units_needed = declared_events / events_per_unit
    return max(1, round(units_needed / order_count))


class MesGenerator:
    """Streams unit-level MES events for a set of production orders."""

    def __init__(
        self,
        order_plan: dict[str, dict[str, Any]],
        steps: Sequence[dict[str, Any]],
        failure_codes: Sequence[str],
        max_rework_attempts: int,
        seed: int,
        period_end: dt.date,
    ) -> None:
        self.order_plan = order_plan
        self.steps = list(steps)
        self.failure_codes = list(failure_codes)
        self.max_rework_attempts = max_rework_attempts
        self.period_end = period_end
        # A seed of its own, rather than the shared generator the other
        # extracts draw from.
        #
        # Two reasons, both found by testing. Consuming the stream twice gave
        # 60,767 events and then 60,677, because the shared generator had
        # advanced -- so the extract was not reproducible, and the control
        # totals agreed with whatever a pass happened to produce. And deriving
        # MES from the shared state would make every MES figure change if an
        # earlier generator's draw count changed.
        #
        # Offset from the run seed so MES does not replay the same sequence as
        # the inventory slice.
        self.seed = seed
        self.rng: Any = None
        self.outcome = MesOutcome()

    def _station(self, step: dict[str, Any]) -> str:
        count = int(step.get("station_count", 1))
        return f"{step['name'][:3].upper()}-{int(self.rng.integers(1, count + 1)):02d}"

    def _unit_events(
        self, serial: str, order_number: str, material: str, start: dt.datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        """Walk one serial through the route.

        Returns its events and whether it completed. A unit that fails its
        last permitted attempt at any step is scrapped there and does not
        continue -- it cannot pass a later step it never reached.
        """
        events: list[dict[str, Any]] = []
        clock = start
        completed = True

        for sequence, step in enumerate(self.steps, start=1):
            failure_rate = float(step.get("failure_rate", 0.0))
            duration = dt.timedelta(hours=float(step.get("duration_hours", 0.5)))
            passed_this_step = False

            for attempt in range(1, self.max_rework_attempts + 2):
                is_last_attempt = attempt == self.max_rework_attempts + 1
                failed = bool(self.rng.random() < failure_rate)
                # A unit out of rework attempts is scrapped rather than
                # retried for ever.
                result = RESULT_FAIL if failed else RESULT_PASS

                finished = clock + duration
                events.append(
                    {
                        "serial_number": serial,
                        "order_number": order_number,
                        "material_number": material,
                        "step_name": step["name"],
                        "step_sequence": sequence,
                        "attempt": attempt,
                        "station": self._station(step),
                        "started_at": clock,
                        "finished_at": finished,
                        "result": result,
                        "failure_code": (
                            self.failure_codes[int(self.rng.integers(0, len(self.failure_codes)))]
                            if failed
                            else None
                        ),
                        "is_final_attempt": is_last_attempt or not failed,
                        "operator": f"OP{int(self.rng.integers(1, 40)):03d}",
                    }
                )
                clock = finished + dt.timedelta(hours=int(self.rng.integers(*INTER_STEP_HOURS)))

                if not failed:
                    passed_this_step = True
                    break
                if is_last_attempt:
                    break

            if not passed_this_step:
                # Scrapped at this step; it never reaches the later ones.
                completed = False
                break

        return events, completed

    def stream(self) -> Iterator[list[dict[str, Any]]]:
        """Yield event chunks, never holding the whole extract in memory.

        Re-seeds and resets the outcome on every call, so the stream is a
        reproducible view of the same data rather than a one-shot generator.
        A second pass over a spent generator would otherwise write an empty
        file -- and the control totals, computed from what was written, would
        agree with it.
        """
        import numpy as np

        self.rng = np.random.default_rng(self.seed)
        self.outcome.reset()
        buffer: list[dict[str, Any]] = []

        for order_number in sorted(self.order_plan):
            plan = self.order_plan[order_number]
            quantity = int(plan["target_quantity"])
            material = plan["material_number"]
            start_date = plan["start_date"]

            for index in range(quantity):
                serial = f"{order_number}-{index + 1:05d}"
                # Units of an order are started through its first day, so
                # stations are busy rather than every unit starting at once.
                start = dt.datetime.combine(
                    start_date, dt.time(hour=int(self.rng.integers(6, 18)))
                ) + dt.timedelta(minutes=int(self.rng.integers(0, 55)))

                events, completed = self._unit_events(serial, order_number, material, start)
                buffer.extend(events)

                self.outcome.units_started += 1
                self.outcome.event_count += len(events)
                if completed:
                    self.outcome.completed_by_order[order_number] = (
                        self.outcome.completed_by_order.get(order_number, 0) + 1
                    )
                else:
                    self.outcome.scrapped_by_order[order_number] = (
                        self.outcome.scrapped_by_order.get(order_number, 0) + 1
                    )

                if len(buffer) >= CHUNK_SIZE:
                    yield buffer
                    buffer = []

        if buffer:
            yield buffer
