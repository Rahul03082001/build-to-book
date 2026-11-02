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
from collections.abc import Iterable, Sequence
from typing import Any

# Two decimal places, half-up: how a ledger rounds. Python's default is
# half-even, which would disagree with the GL by a cent on ties.
MONEY = decimal.Decimal("0.01")


def money(value: decimal.Decimal | int | str) -> decimal.Decimal:
    """Quantise to two decimal places, rounding half-up."""
    return decimal.Decimal(value).quantize(MONEY, rounding=decimal.ROUND_HALF_UP)


class _HashingTee:
    """A text sink that forwards writes and hashes them on the way through.

    `csv.writer` only needs `.write`, so this keeps the digest and the file
    byte-for-byte in agreement without a second pass over the data.
    """

    __slots__ = ("_digest", "_fh")

    def __init__(self, fh: Any, digest: Any) -> None:
        self._fh = fh
        self._digest = digest

    def write(self, text: str) -> int:
        self._digest.update(text.encode("utf-8"))
        return self._fh.write(text)


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


def write_extract_streaming(
    spec: ExtractSpec,
    chunks: Iterable[Sequence[dict[str, Any]]],
    out_dir: pathlib.Path,
    generated_at: dt.datetime,
) -> WrittenExtract:
    """Write an extract from an iterable of row chunks.

    Peak memory is a function of chunk size, not row count. The 5M-row MES
    feed held as Python dicts would be about 4.3 GB -- measured at 929 bytes
    per row on the inventory slice -- which is why generation streams
    (ADR-024, BTB-36).

    The row count, value total and SHA-256 accumulate as chunks are written,
    so nothing is computed by iterating the data a second time. The hash is
    computed from the bytes as they are written rather than by re-reading the
    finished file, which also means it covers exactly what was written.

    Args:
        spec: The extract's shape.
        chunks: Iterable of row-dict sequences. May be a generator.
        out_dir: Directory to write into.
        generated_at: Run timestamp for the sidecar. The CSV carries no
            timestamp, so two runs with the same seed are byte-identical.

    Returns:
        What was written, including the file's SHA-256.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{spec.name}.csv"

    expected = set(spec.columns)
    columns = list(spec.columns)

    row_count = 0
    value_total = decimal.Decimal("0") if spec.value_column is not None else None
    digest = hashlib.sha256()

    # newline="" plus an explicit \r\n keeps the bytes identical on every
    # platform, which the determinism test depends on.
    with path.open("w", newline="", encoding="utf-8") as fh:
        # Tee every write into the hash. Hashing here rather than re-reading
        # the finished file means the digest covers exactly the bytes csv
        # produced -- and at 5M rows, re-reading would double the I/O.
        tee = _HashingTee(fh, digest)
        writer = csv.writer(tee, lineterminator="\r\n")
        writer.writerow(columns)

        for chunk in chunks:
            for index, row in enumerate(chunk):
                unexpected = set(row) - expected
                if unexpected:
                    raise ValueError(
                        f"{spec.name} row {row_count + index} has unexpected "
                        f"columns: {sorted(unexpected)}"
                    )
                missing = expected - set(row)
                if missing:
                    raise ValueError(
                        f"{spec.name} row {row_count + index} is missing columns: {sorted(missing)}"
                    )
                if value_total is not None:
                    amount = row[spec.value_column]
                    if isinstance(amount, decimal.Decimal) and amount.as_tuple().exponent < -2:
                        raise ValueError(
                            f"{spec.name} row {row_count + index}: "
                            f"{spec.value_column}={amount} has more than two decimal "
                            f"places; apply money() before writing so the extract and "
                            f"its control total re-add to the same value"
                        )
                    value_total += amount
                writer.writerow([_render(row[column]) for column in columns])
            row_count += len(chunk)

    if value_total is not None:
        value_total = money(value_total)

    sha256 = digest.hexdigest()
    control_totals_path = _write_control_totals(
        spec, out_dir, row_count, value_total, sha256, generated_at
    )

    return WrittenExtract(
        spec=spec,
        path=path,
        control_totals_path=control_totals_path,
        row_count=row_count,
        value_total=value_total,
        sha256=sha256,
    )


def _write_control_totals(
    spec: ExtractSpec,
    out_dir: pathlib.Path,
    row_count: int,
    value_total: decimal.Decimal | None,
    sha256: str,
    generated_at: dt.datetime,
) -> pathlib.Path:
    """Write the sidecar. One implementation, used by both write paths."""
    control_totals_path = out_dir / f"{spec.name}.control_totals.json"
    payload: dict[str, Any] = {
        "extract": spec.name,
        "source_system": spec.source_system,
        "row_count": row_count,
        "value_column": spec.value_column,
        "value_total": str(value_total) if value_total is not None else None,
        "natural_key": list(spec.natural_key),
        "sha256": sha256,
        "generated_at": generated_at.replace(microsecond=0).isoformat(),
        "notice": "SYNTHETIC DATA. Simulated, SOX-style controls. Not SOX compliant.",
    }
    control_totals_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return control_totals_path


def write_extract(
    spec: ExtractSpec,
    rows: Sequence[dict[str, Any]],
    out_dir: pathlib.Path,
    generated_at: dt.datetime,
) -> WrittenExtract:
    """Write one extract and its control-totals sidecar.

    Convenience wrapper over `write_extract_streaming` for callers that
    already hold every row -- the small extracts, and the tests. It delegates
    rather than duplicating, so the streamed and in-memory paths cannot
    produce different bytes for the same data; a test asserts they agree.

    Args:
        spec: The extract's shape.
        rows: Row dicts. Every key in ``spec.columns`` must be present; an
            unexpected key is an error rather than being dropped, because a
            silently ignored column is how a field goes missing downstream.
        out_dir: Directory to write into.
        generated_at: Run timestamp, recorded in the sidecar.

    Returns:
        What was written, including the file's SHA-256.
    """
    return write_extract_streaming(spec, [rows], out_dir, generated_at)


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
