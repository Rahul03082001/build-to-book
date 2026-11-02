"""Load and validate generator/config.yaml.

Kept separate from the generation code so that tests, ingestion, and the
controls can all read the same declared profiles, freshness windows, and
defect switches without importing the generator itself.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
from typing import Any

import yaml

CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"

VALID_PROFILES = ("demo", "full")


@dataclasses.dataclass(frozen=True)
class Profile:
    """Volume settings for one data profile (ADR-016)."""

    name: str
    description: str
    months: int
    period_start: dt.date
    materials: int
    suppliers: int
    rack_configurations: int
    purchase_order_lines: int
    production_orders: int
    mes_events: int
    gl_postings: int

    @property
    def is_hosted(self) -> bool:
        """Whether this profile is small enough for the free-tier database."""
        return self.name == "demo"


@dataclasses.dataclass(frozen=True)
class GeneratorConfig:
    """The whole of generator/config.yaml, parsed."""

    seed: int
    profiles: dict[str, Profile]
    domain: dict[str, Any]
    close_calendar: dict[str, Any]
    freshness_hours: dict[str, int]
    defects: dict[str, dict[str, Any]]
    output: dict[str, Any]

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            raise KeyError(f"unknown profile {name!r}; expected one of {VALID_PROFILES}")
        return self.profiles[name]

    def enabled_defects(self) -> list[str]:
        """Defect keys currently switched on, in config order."""
        return [key for key, spec in self.defects.items() if spec.get("enabled", False)]


def load(path: pathlib.Path | None = None) -> GeneratorConfig:
    """Parse the generator config, failing loudly on a malformed file."""
    path = path or CONFIG_PATH
    raw = yaml.safe_load(path.read_text())

    missing = {
        "seed",
        "profiles",
        "domain",
        "close_calendar",
        "freshness_hours",
        "defects",
        "output",
    } - set(raw)
    if missing:
        raise ValueError(f"{path.name} is missing required keys: {sorted(missing)}")

    profiles: dict[str, Profile] = {}
    for name, body in raw["profiles"].items():
        period_start = body["period_start"]
        if isinstance(period_start, str):
            period_start = dt.date.fromisoformat(period_start)
        profiles[name] = Profile(
            name=name,
            description=body["description"],
            months=int(body["months"]),
            period_start=period_start,
            materials=int(body["materials"]),
            suppliers=int(body["suppliers"]),
            rack_configurations=int(body["rack_configurations"]),
            purchase_order_lines=int(body["purchase_order_lines"]),
            production_orders=int(body["production_orders"]),
            mes_events=int(body["mes_events"]),
            gl_postings=int(body["gl_postings"]),
        )

    for expected in VALID_PROFILES:
        if expected not in profiles:
            raise ValueError(f"{path.name} must define the {expected!r} profile (ADR-016)")

    return GeneratorConfig(
        seed=int(raw["seed"]),
        profiles=profiles,
        domain=raw["domain"],
        close_calendar=raw["close_calendar"],
        freshness_hours=raw["freshness_hours"],
        defects=raw["defects"],
        output=raw["output"],
    )
