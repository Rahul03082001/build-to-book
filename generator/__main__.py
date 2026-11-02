"""Generate the synthetic source extracts.

    python -m generator --profile demo
    python -m generator --profile full --out /tmp/btb-full

Writes one CSV per extract plus a control-totals sidecar (ADR-022), and prints
a summary. All output is synthetic (ADR-001, ADR-019).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import pathlib
import sys

from generator import config as genconfig
from generator import inventory, writers

BANNER = "SYNTHETIC DATA -- simulated, SOX-style controls. Not SOX compliant."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m generator",
        description="Generate synthetic manufacturing source extracts.",
    )
    parser.add_argument(
        "--profile",
        default="demo",
        choices=genconfig.VALID_PROFILES,
        help="Data profile: demo is small enough to host, full is local only (ADR-016).",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=None,
        help="Output directory. Defaults to the generator config's output.directory.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override the configured seed. The same seed reproduces identical extracts.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = genconfig.load()

    if args.seed is not None:
        cfg = dataclasses.replace(cfg, seed=args.seed)

    out_dir = args.out or pathlib.Path(cfg.output["directory"])
    # One timestamp for the whole run, so every sidecar agrees.
    generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0, tzinfo=None)

    enabled = cfg.enabled_defects()

    print(BANNER)
    print(f"profile={args.profile}  seed={cfg.seed}  out={out_dir}")
    print(f"defects enabled: {', '.join(enabled) if enabled else 'none (clean run)'}")
    print()

    generator = inventory.InventoryGenerator(cfg, args.profile)
    data = generator.generate()

    print(f"{'extract':28}{'rows':>8}  {'value total':>20}  sha256")
    print("-" * 86)
    written: list[writers.WrittenExtract] = []
    for name, rows in data.as_mapping().items():
        result = writers.write_extract(
            spec=inventory.EXTRACT_SPECS[name],
            rows=rows,
            out_dir=out_dir,
            generated_at=generated_at,
        )
        written.append(result)
        total = f"{result.value_total:,}" if result.value_total is not None else "-"
        print(f"{name:28}{result.row_count:>8}  {total:>20}  {result.sha256[:12]}")

    print("-" * 86)
    print(f"{'total':28}{sum(w.row_count for w in written):>8}")
    print()
    print(f"Wrote {len(written)} extracts and {len(written)} control-totals sidecars to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
