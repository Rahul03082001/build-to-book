"""Run the reconciliations, and optionally publish if they all passed.

    python -m controls                      # run the checks, report, record
    python -m controls --publish            # ... and publish if clean
    python -m controls --schema sandbox     # check the failure-demo schema

Exits non-zero when a critical check fails, so CI and the nightly run treat a
failed control as a failed pipeline (ADR-010).
"""

from __future__ import annotations

import argparse
import sys

from controls import publish as publish_module
from controls import runner
from ingestion import db, loader

BANNER = "SYNTHETIC DATA -- simulated, SOX-style controls. Not SOX compliant."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m controls",
        description="Run the reconciliations and optionally publish.",
    )
    parser.add_argument(
        "--schema-prefix",
        default="",
        choices=sorted(runner.ALLOWED_SCHEMA_PREFIXES),
        help=(
            "Which build to check. Empty is the live build; sandbox_ is the "
            "failure demo, which reads sandbox_staging/intermediate/marts and "
            "never production."
        ),
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Batch to assess. Defaults to the most recent successful load.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Swap into reporting if every critical check passed.",
    )
    parser.add_argument(
        "--target-schema",
        default="reporting",
        help="Where --publish writes. The failure demo must not target reporting.",
    )
    return parser


def _latest_batch(conn) -> str | None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT batch_id FROM audit.pipeline_runs WHERE status = 'success' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
    return row[0] if row else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commit = loader.git_commit_sha()

    with db.connect() as conn:
        batch_id = args.batch_id or _latest_batch(conn)
        if batch_id is None:
            print("No successful load found. Run `make load` first.", file=sys.stderr)
            return 1

        print(BANNER)
        print(f"schemas={runner.layer_schemas(args.schema_prefix)}  batch={batch_id}")
        print(f"commit={commit[:12]}")
        print()

        summary = runner.run_checks(conn, args.schema_prefix, batch_id)

        header = f"{'control':10}{'period':24}{'result':8}check"
        print(header)
        print("-" * 100)
        for result in summary.results:
            print(
                f"{result.check.check_id:10}{(result.period or '-'):24}"
                f"{'PASS' if result.passed else 'FAIL':8}{result.check.name}"
            )
            if result.detail:
                print(f"{'':42}{result.detail[:140]}")
        print("-" * 100)
        print(
            f"{summary.checks_passed}/{summary.checks_run} passed, "
            f"{len(summary.blocking_failures)} blocking, "
            f"{len(summary.warnings)} warning(s)"
        )
        print()

        if not args.publish:
            if summary.blocking_failures:
                print("CRITICAL CHECKS FAILED -- a publish would be blocked.", file=sys.stderr)
                return 1
            print("All checks passed. Run with --publish to swap into reporting.")
            return 0

        result = publish_module.publish(conn, summary, commit, target_schema=args.target_schema)

        if result.published:
            print(
                f"PUBLISHED to {result.target_schema}: version {result.published_version}"
                + (f" (was {result.previous_version})" if result.previous_version else "")
            )
            return 0

        print(f"PUBLISH BLOCKED: {result.reason}", file=sys.stderr)
        print(
            f"{result.target_schema} still serves "
            f"{result.published_version or 'no dataset'} -- the last good dataset.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
