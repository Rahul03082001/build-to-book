"""Validate and incrementally load the generated extracts into `raw`.

    python -m ingestion
    python -m ingestion --data-dir data --extract goods_movements
    python -m ingestion --reset           # empty raw first, then load

Every file in one invocation shares a batch id. A rerun inserts nothing and
reports what it skipped (ADR-020), and each file's outcome is recorded in
`audit.pipeline_runs` whether it succeeded or failed (ADR-008).

`--reset` exists because insert-only loading cannot backfill. The anti-join
matches on the natural key, so a row already in `raw` is skipped whatever its
other columns say -- which means a source that gains a **column** loads
cleanly and leaves every existing row holding a null. That is ADR-020's
recorded limitation, and it bit on BTB-34: `goods_movements` gained
`inbound_delivery`, 1,242 receipts skipped, and all 1,242 kept a null delivery
while the extract on disk had the value.

The audit trail is not touched. It is append-only and a TRUNCATE on it is
blocked by a statement-level trigger (ADR-008), so the history of what was
loaded survives a reset of what was loaded into.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

from generator import dataset
from ingestion import db, loader, schemas

BANNER = "SYNTHETIC DATA -- simulated, SOX-style controls. Not SOX compliant."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion",
        description="Validate and load source extracts into raw.",
    )
    parser.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=pathlib.Path("data"),
        help="Directory holding the extracts and their control-totals sidecars.",
    )
    parser.add_argument(
        "--extract",
        action="append",
        choices=sorted(schemas.SCHEMAS),
        help="Load only these extracts. Repeatable. Default: all of them.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Reuse a batch id instead of generating one. Mainly for reruns.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Empty the raw tables before loading. Needed when an extract gains "
            "a column, because an incremental load cannot backfill one. Does "
            "not touch the append-only audit trail."
        ),
    )
    return parser


# Taken from the generator rather than restated here. Two lists would drift the
# moment a source system is added -- and the symptom would be an extract that
# generates fine and silently never loads.
LOAD_ORDER = list(dataset.LOAD_ORDER)


def reset_raw(connection: Any, extracts: Sequence[str]) -> int:
    """Empty the given raw tables. Returns how many were emptied.

    One statement so the tables come back empty together: truncating them one
    at a time would leave a window where `goods_movements` is empty and
    `purchase_order_lines` is not, and anything reading raw in between would
    see a referentially broken warehouse.

    Table names come from the declared load order, never from user input, so
    there is nothing here to interpolate unsafely.
    """
    if not extracts:
        return 0
    unknown = [name for name in extracts if name not in LOAD_ORDER]
    if unknown:
        raise ValueError(f"not declared in the load order: {unknown}")
    targets = ", ".join(f"raw.{name}" for name in extracts)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {targets}")
    return len(extracts)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested = args.extract or LOAD_ORDER
    extracts = [name for name in LOAD_ORDER if name in requested]

    batch_id = args.batch_id or loader.new_batch_id()
    commit = loader.git_commit_sha()

    print(BANNER)
    print(f"batch={batch_id}  commit={commit[:12]}  data={args.data_dir}")
    if args.reset:
        with db.connect() as connection:
            emptied = reset_raw(connection, extracts)
        print(f"reset: emptied {emptied} raw table(s) before loading")
    print()
    header = f"{'extract':24}{'in file':>9}{'inserted':>10}{'skipped':>9}{'rejected':>10}  complete"
    print(header)
    print("-" * len(header))

    totals = {"in_file": 0, "inserted": 0, "skipped": 0, "rejected": 0}
    incomplete: list[str] = []
    failures: list[str] = []

    with db.connect(db.LOADER_ROLE) as conn:
        for name in extracts:
            csv_path = args.data_dir / f"{name}.csv"
            if not csv_path.exists():
                print(f"{name:24}{'MISSING':>9}")
                failures.append(f"{name}: {csv_path} not found")
                continue
            try:
                result = loader.load_extract(conn, name, csv_path, batch_id, commit_sha=commit)
            except Exception as error:
                print(f"{name:24}{'FAILED':>9}  {type(error).__name__}")
                failures.append(f"{name}: {error}")
                continue

            totals["in_file"] += result.rows_in_file
            totals["inserted"] += result.rows_inserted
            totals["skipped"] += result.rows_skipped
            totals["rejected"] += result.rows_rejected
            if not result.completeness_ok:
                incomplete.append(f"{name} ({result.completeness_detail})")

            print(
                f"{name:24}{result.rows_in_file:>9}{result.rows_inserted:>10}"
                f"{result.rows_skipped:>9}{result.rows_rejected:>10}  "
                f"{'yes' if result.completeness_ok else 'NO'}"
            )

    print("-" * len(header))
    print(
        f"{'total':24}{totals['in_file']:>9}{totals['inserted']:>10}"
        f"{totals['skipped']:>9}{totals['rejected']:>10}"
    )
    print()

    if totals["rejected"]:
        print(
            f"{totals['rejected']} row(s) rejected. They are in audit.rejected_rows with the "
            f"rule they broke -- nothing was silently dropped."
        )

    # A completeness shortfall means rows vanished between the file and the
    # warehouse. That is a CTRL-001 finding and must not exit zero.
    if incomplete:
        print("COMPLETENESS FAILED (CTRL-001):", file=sys.stderr)
        for problem in incomplete:
            print(f"  {problem}", file=sys.stderr)
    if failures:
        print("LOAD FAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)

    return 1 if (incomplete or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
