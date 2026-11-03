#!/usr/bin/env python
"""Explain one plantable defect: what it simulates, and whether it can run yet.

    scripts/explain_defect.py backdated_gl_posting
    scripts/explain_defect.py --list

Exists because the failure-demo workflow's `defect` dropdown is generated from
the defect **registry**, which is the full specification -- all thirteen
defects the project intends to plant. Only some have injectors; the rest arrive
in later phases. So the menu offers choices that cannot run, and the workflow
used to answer any of them with a bare `exit 1`.

A placeholder that fails is fine. A placeholder that fails without saying why
is not: the reader cannot tell "this defect is not built yet" from "the demo is
not built yet" from "I picked the wrong thing". This prints which it is.

The logic lives here rather than inline in the workflow because a
`workflow_dispatch`-only job never runs in CI, so a mistake in it stays
invisible until someone clicks the button -- which is the exact failure being
fixed. Here it is covered by tests.

Phase 6 (BTB-67) keeps this as the demo's preflight step: the real demo has to
refuse an unimplemented defect before it spends ten minutes building a
warehouse to plant nothing.

Exit codes:
    0  the defect is known and has an injector -- it can be planted today
    3  the defect is known but its injector is not built yet
    2  the defect is not in the registry at all
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_READY = 0
EXIT_UNKNOWN = 2
EXIT_NOT_IMPLEMENTED = 3


def _annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation, but only when running in one.

    Outside Actions the `::warning::` prefix is noise, so the same script is
    readable from a terminal.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")
    else:
        print(f"{level}: {message}")


def explain(key: str) -> int:
    """Describe one defect and report whether it can be planted."""
    from generator import defects

    if key not in defects.REGISTRY:
        _annotate("error", f"{key!r} is not a defect in the registry.")
        print()
        print("Known defects:")
        for known in sorted(defects.REGISTRY):
            print(f"  {known}")
        return EXIT_UNKNOWN

    spec = defects.REGISTRY[key]
    implemented = key in defects.INJECTORS

    print(f"defect:            {spec.key}")
    print(f"title:             {spec.title}")
    print(f"simulates:         {spec.simulates}")
    print(f"target extract:    {spec.target_extract}")
    print(f"expected control:  {spec.expected_control}")
    print(f"control acts:      {'preventively, at load' if spec.preventive else 'after landing'}")
    print(f"injector phase:    {spec.phase}")
    print()

    if implemented:
        print("This defect has an injector and can be planted today:")
        # `make data DEFECTS=<key>`, not `make data-defects`: the latter takes
        # no DEFECTS and plants `--defect all`, so printing it here would hand
        # the reader a command that quietly does something broader than it
        # says. Caught by running the command.
        print(f"  make data DEFECTS={spec.key}")
        print(f"  python -m generator --profile demo --defect {spec.key}")
        return EXIT_READY

    # The registry's own message names the phase, so it cannot drift from the
    # code that raises it.
    try:
        defects.injector_for(key)
    except NotImplementedError as error:
        _annotate("warning", str(error))
    else:  # pragma: no cover - contradicts `implemented` being False
        raise AssertionError(f"{key} is absent from INJECTORS but injector_for succeeded")

    print()
    print(
        f"It is listed because the registry is the full specification of what "
        f"this project intends to plant -- {len(defects.REGISTRY)} defects, of "
        f"which {len(defects.INJECTORS)} are built. Choosing it is not a "
        f"mistake; the injector simply arrives in phase {spec.phase}."
    )
    print()
    print("Implemented today:")
    for known in sorted(defects.INJECTORS):
        print(f"  {known}")
    return EXIT_NOT_IMPLEMENTED


def list_defects() -> int:
    """Every registered defect, with whether its injector exists."""
    from generator import defects

    print(f"{'defect':36}{'control':11}{'phase':>6}  injector")
    print("-" * 70)
    for key in sorted(defects.REGISTRY):
        spec = defects.REGISTRY[key]
        state = "built" if key in defects.INJECTORS else f"phase {spec.phase}"
        print(f"{key:36}{spec.expected_control:11}{spec.phase:>6}  {state}")
    print("-" * 70)
    print(f"{len(defects.INJECTORS)} of {len(defects.REGISTRY)} injectors built")
    return EXIT_READY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/explain_defect.py",
        description="Explain a plantable defect and whether its injector is built.",
    )
    parser.add_argument("defect", nargs="?", help="The defect key to explain.")
    parser.add_argument(
        "--list", action="store_true", help="List every defect and its injector state."
    )
    args = parser.parse_args(argv)

    if args.list:
        return list_defects()
    if not args.defect:
        parser.error("give a defect key, or --list")
    return explain(args.defect)


if __name__ == "__main__":
    sys.exit(main())
