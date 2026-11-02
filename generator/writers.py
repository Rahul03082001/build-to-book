"""Write extracts as CSV with a control-totals sidecar (ADR-022).

Each extract gets two files:

    goods_movements.csv
    goods_movements.control_totals.json

The sidecar is what a source system's trailer record would carry -- row count
and value total -- so completeness can be checked at load (CTRL-001). It is a
sidecar rather than a trailer row inside the CSV because the audit trail, the
completeness control and the rejected-row log all work one file at a time, and
a trailer row would mean every reader strips a special case before parsing.

Money is written as a plain decimal string, never a float. A float total will
not re-add to the same value the loader computes, and a completeness control
that fails on rounding is worse than no control at all.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import pathlib
from collections.abc import Sequence
from typing import Any

# Two decimal places, half-up: how a ledger rounds. Python's default is
# half-even, which would disagree with the GL by a cent on ties.
MONEY = decimal.Decimal("0.01")


def money(value: decimal.Decimal | int | str) -> decimal.Decimal:
    """Quantise to two decimal places, rounding half-up."""
    return decimal.Decimal(value).quantize(MONEY, rounding=decimal.ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True)
class ExtractSpec:
    """How one extract is written and summarised.

    Attributes:
        name: File stem, also the raw table name.
        columns: Column order. Explicit so the CSV is stable across runs and
            a reordering shows up as a diff rather than silently changing the
            file.
        source_system: Recorded in the sidecar, so the audit trail can say
            which system a row came from.
        value_column: Column summed into the sidecar's value total, or None
            where the extract has no monetary amount. Master data has none,
            and a sidecar claiming a total of 0.00 would be a lie.
        natural_key: Columns identifying a row in the source (ADR-017,
            ADR-020). The loader's anti-join uses exactly these.
    """

    name: str
    columns: Sequence[str]
    source_system: str
    value_column: str | None
    natural_key: Sequence[str]


@dataclasses.dataclass(frozen=True)
class WrittenExtract:
    """What was written, for the run summary and the tests."""

    spec: ExtractSpec
    path: pathlib.Path
    control_totals_path: pathlib.Path
    row_count: int
    value_total: decimal.Decimal | None
    sha256: str


def _render(value: Any) -> str:
    """Render one cell deterministically.

    Floats are refused outright: a float in a money or quantity column is how
    a reconciliation ends up out by a cent, and the failure surfaces far from
    its cause.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        raise TypeError(
            "floats are not written to extracts; use Decimal for money and int "
            "for quantities, so totals re-add exactly"
        )
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def write_extract(
    spec: ExtractSpec,
    rows: Sequence[dict[str, Any]],
    out_dir: pathlib.Path,
    generated_at: dt.datetime,
) -> WrittenExtract:
    """Write one extract and its control-totals sidecar.

    Args:
        spec: The extract's shape.
        rows: Row dicts. Every key in ``spec.columns`` must be present; an
            unexpected key is an error rather than being dropped, because a
            silently ignored column is how a field goes missing downstream.
        out_dir: Directory to write into.
        generated_at: Run timestamp, recorded in the sidecar. The CSV itself
            carries no run timestamp, so two runs with the same seed produce
            byte-identical CSVs.

    Returns:
        What was written, including the file's SHA-256.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{spec.name}.csv"

    expected = set(spec.columns)
    for index, row in enumerate(rows):
        unexpected = set(row) - expected
        if unexpected:
            raise ValueError(
                f"{spec.name} row {index} has unexpected columns: {sorted(unexpected)}"
            )
        missing = expected - set(row)
        if missing:
            raise ValueError(f"{spec.name} row {index} is missing columns: {sorted(missing)}")

    # newline="" plus an explicit \r\n keeps the bytes identical on every
    # platform, which the determinism test depends on.
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\r\n")
        writer.writerow(spec.columns)
        for row in rows:
            writer.writerow([_render(row[column]) for column in spec.columns])

    value_total: decimal.Decimal | None = None
    if spec.value_column is not None:
        # The value column must already be ledger-rounded to two places. If the
        # CSV carries 10.005 while the sidecar carries 10.01, a loader that sums
        # the file disagrees with the control total by a cent and CTRL-001 fails
        # for a rounding reason rather than a completeness one -- which is how a
        # real control gets switched off. Quantising silently here would hide
        # the generator bug instead, so this is an error.
        for index, row in enumerate(rows):
            amount = row[spec.value_column]
            if isinstance(amount, decimal.Decimal) and amount.as_tuple().exponent < -2:
                raise ValueError(
                    f"{spec.name} row {index}: {spec.value_column}={amount} has more than "
                    f"two decimal places; apply money() before writing so the extract and "
                    f"its control total re-add to the same value"
                )
        value_total = money(sum((row[spec.value_column] for row in rows), decimal.Decimal("0")))

    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    control_totals_path = out_dir / f"{spec.name}.control_totals.json"
    payload: dict[str, Any] = {
        "extract": spec.name,
        "source_system": spec.source_system,
        "row_count": len(rows),
        "value_column": spec.value_column,
        "value_total": str(value_total) if value_total is not None else None,
        "natural_key": list(spec.natural_key),
        "sha256": digest,
        "generated_at": generated_at.replace(microsecond=0).isoformat(),
        "notice": "SYNTHETIC DATA. Simulated, SOX-style controls. Not SOX compliant.",
    }
    control_totals_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    return WrittenExtract(
        spec=spec,
        path=path,
        control_totals_path=control_totals_path,
        row_count=len(rows),
        value_total=value_total,
        sha256=digest,
    )


def read_control_totals(path: pathlib.Path) -> dict[str, Any]:
    """Read a sidecar. A missing or unparseable sidecar is a hard failure.

    Treating it as a skip would make CTRL-001 bypassable by deleting a file
    (ADR-022).
    """
    if not path.exists():
        raise FileNotFoundError(
            f"control totals missing for {path.name}: completeness cannot be checked, "
            f"so the load must fail rather than proceed unchecked"
        )
    return json.loads(path.read_text())
