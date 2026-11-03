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
from generator import dataset as dataset_module
from generator import defects

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
        "--defect",
        action="append",
        default=None,
        metavar="KEY",
        help=(
            "Plant this defect. Repeatable. Overrides the switches in "
            "config.yaml, which all default to off. Use `all` for every "
            "implemented defect."
        ),
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

    from generator import defects as defect_registry

    if args.defect:
        requested = sorted(defect_registry.INJECTORS) if "all" in args.defect else list(args.defect)
    else:
        requested = cfg.enabled_defects()

    unknown = [key for key in requested if key not in defect_registry.REGISTRY]
    if unknown:
        print(f"Unknown defect(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Known: {', '.join(sorted(defect_registry.REGISTRY))}", file=sys.stderr)
        return 2

    enabled = requested

    print(BANNER)
    print(f"profile={args.profile}  seed={cfg.seed}  out={out_dir}")
    print(f"defects enabled: {', '.join(enabled) if enabled else 'none (clean run)'}")
    print()

    data, planted = dataset_module.build(cfg, args.profile, enabled)

    manifest = defects.DefectManifest(
        seed=cfg.seed, profile=args.profile, generated_at=generated_at
    )
    for record in planted:
        manifest.add(record)

    print(f"{'extract':28}{'rows':>8}  {'value total':>20}  sha256")
    print("-" * 86)
    written = dataset_module.write_all(data, out_dir, generated_at)
    for result in written:
        total = f"{result.value_total:,}" if result.value_total is not None else "-"
        print(f"{result.spec.name:28}{result.row_count:>8}  {total:>20}  {result.sha256[:12]}")

    print("-" * 86)
    print(f"{'total':28}{sum(w.row_count for w in written):>8}")
    print()

    manifest_path = out_dir / cfg.output["manifest_filename"]
    manifest.write(manifest_path)
    print(f"Wrote {len(written)} extracts and {len(written)} control-totals sidecars to {out_dir}")

    if enabled:
        print()
        print(f"PLANTED {manifest.total_injected} record(s) across {len(enabled)} defect(s):")
        for key, count in sorted(manifest.to_dict()["counts_by_defect"].items()):
            print(f"  {key:36} {count:>5} record(s)")
        print(f"Manifest: {manifest_path}")
        print("Controls are expected to detect these. A clean run plants nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
