"""Validated, incremental, idempotent loading into `raw`.

The load path, per file:

1. Read the control-totals sidecar. A missing sidecar fails the load rather
   than skipping the completeness check (ADR-022).
2. Hash the file's bytes **before** parsing, so the checksum describes what
   arrived rather than what we managed to read.
3. Validate against the declared schema. Failing rows are quarantined with the
   rule they broke, never dropped (BTB-17).
4. Reject rows that duplicate their natural key *within the file* -- an
   upstream interface posting twice (BTB-76).
5. Stage the survivors in a per-batch table, then insert into the target with
   an anti-join on the natural key (ADR-020).
6. Record one row in `audit.pipeline_runs`, on the failure path as well as the
   success path.

Why staging plus an anti-join rather than `ON CONFLICT DO NOTHING`: Postgres
reports only how many rows it inserted, not which it discarded. That makes a
routine rerun and a genuinely duplicated source row indistinguishable, and the
duplicate-receipt control would have nothing to detect. Here the three
outcomes are counted separately:

    rows_inserted  new rows
    rows_skipped   already present from an earlier run -- a rerun, not a fault
    rows_rejected  invalid, or duplicated inside the file -- an upstream fault
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import hashlib
import json
import math
import pathlib
import re
import subprocess
import uuid
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import pandera.errors as pa_errors
import psycopg
from psycopg import sql

from ingestion import (
    BATCH_ID_COLUMN,
    FILE_SHA256_COLUMN,
    LOADED_AT_COLUMN,
    SOURCE_FILE_COLUMN,
    schemas,
)

RAW_SCHEMA = "raw"
AUDIT_SCHEMA = "audit"

REJECT_SCHEMA_VIOLATION = "schema_violation"
REJECT_DUPLICATE_NATURAL_KEY = "duplicate_natural_key"

# A batch id must be sortable by time and safe to embed in an identifier.
_IDENTIFIER_SAFE = re.compile(r"[^0-9a-zA-Z]+")


def new_batch_id(now: dt.datetime | None = None) -> str:
    """A unique, time-sortable batch id.

    The timestamp prefix makes batches sort chronologically; the suffix stops
    two runs started in the same second from colliding, which happens in tests
    and in a retry loop.
    """
    now = now or dt.datetime.now(dt.UTC)
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def git_commit_sha() -> str:
    """The commit that is running, recorded alongside every load (ADR-008).

    Returns ``"unknown"`` outside a git checkout rather than failing: the audit
    row is more valuable than the provenance field is complete, and an honest
    "unknown" is better than refusing to record the run at all.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=pathlib.Path(__file__).resolve().parent.parent,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def file_sha256(path: pathlib.Path) -> str:
    """Hash the file's bytes, in chunks so the full profile does not load it all."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclasses.dataclass
class RejectedRow:
    source_row: int
    rule: str
    detail: str
    raw_row: dict[str, Any]


@dataclasses.dataclass
class LoadResult:
    """What one file's load did. Mirrors a row of audit.pipeline_runs."""

    run_id: uuid.UUID
    batch_id: str
    extract_name: str
    source_file: str
    file_sha256: str
    git_commit_sha: str
    rows_in_file: int
    rows_inserted: int
    rows_skipped: int
    rows_rejected: int
    control_total_rows: int | None
    control_total_value: decimal.Decimal | None
    started_at: dt.datetime
    ended_at: dt.datetime | None = None
    status: str = "running"
    failure_reason: str | None = None
    # Summed from the file's own rows, for comparison against the declared
    # trailer total. None when the extract carries no monetary column.
    value_total_in_file: decimal.Decimal | None = None

    @property
    def rows_accounted_ok(self) -> bool:
        """Whether every row the source declared is accounted for.

        Inserted + skipped + rejected must equal the declared row count. A
        shortfall means rows vanished between the file and the warehouse.
        """
        if self.control_total_rows is None:
            return True
        return (
            self.rows_inserted + self.rows_skipped + self.rows_rejected == self.control_total_rows
        )

    @property
    def value_total_ok(self) -> bool:
        """Whether the file's value column sums to the declared trailer total.

        Row counts alone miss a whole class of fault: a row whose amount was
        altered in transit keeps the count intact and changes the money. This
        is the half of CTRL-001 that catches it, and it was missing until the
        control matrix was checked against what the loader actually did.

        Compared exactly, not within a tolerance. Both sides are
        two-decimal-place decimals by construction -- the writer refuses
        anything else -- so any difference is a real one.
        """
        if self.control_total_value is None or self.value_total_in_file is None:
            return True
        return self.value_total_in_file == self.control_total_value

    @property
    def completeness_ok(self) -> bool:
        """CTRL-001: both row counts and value totals reconcile to the trailer."""
        return self.rows_accounted_ok and self.value_total_ok

    @property
    def completeness_detail(self) -> str:
        """Why completeness failed, for the operator and the audit trail."""
        problems = []
        if not self.rows_accounted_ok:
            accounted = self.rows_inserted + self.rows_skipped + self.rows_rejected
            problems.append(
                f"row count: {self.rows_inserted} inserted + {self.rows_skipped} skipped "
                f"+ {self.rows_rejected} rejected = {accounted}, but the source "
                f"declared {self.control_total_rows}"
            )
        if not self.value_total_ok:
            assert self.value_total_in_file is not None
            assert self.control_total_value is not None
            problems.append(
                f"value total: the file sums to {self.value_total_in_file} but the "
                f"source declared {self.control_total_value} (difference "
                f"{self.value_total_in_file - self.control_total_value})"
            )
        return "; ".join(problems)


def _staging_table(extract_name: str, batch_id: str) -> sql.Identifier:
    suffix = _IDENTIFIER_SAFE.sub("_", batch_id)
    # Postgres truncates identifiers at 63 bytes; truncating ourselves keeps
    # the name predictable so the `finally` drop always targets the right one.
    return sql.Identifier(f"_stg_{extract_name}_{suffix}"[:63])


def _normalise(value: Any) -> Any:
    """Convert a pandas/numpy scalar to something COPY can render.

    COPY's text format writes whatever ``str()`` produces, so a numpy float
    holding a whole number becomes ``"10.0"`` and Postgres refuses it for an
    integer column. Missing values must become None, not the string "nan".
    """
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _to_db_rows(frame: pd.DataFrame, columns: Sequence[str]) -> list[tuple[Any, ...]]:
    """Convert a validated frame to tuples ready for COPY."""
    selected = frame[list(columns)]
    prepared = selected.astype(object).where(pd.notna(selected), None)
    return [
        tuple(_normalise(value) for value in row)
        for row in prepared.itertuples(index=False, name=None)
    ]


def _read_control_totals(csv_path: pathlib.Path, suffix: str) -> dict[str, Any]:
    sidecar = csv_path.with_suffix("")
    sidecar = sidecar.with_name(sidecar.name + suffix)
    if not sidecar.exists():
        raise FileNotFoundError(
            f"control totals missing for {csv_path.name} (expected {sidecar.name}): "
            f"completeness cannot be checked, so the load fails rather than "
            f"proceeding unchecked (ADR-022)"
        )
    return json.loads(sidecar.read_text())


#: Rows read, validated and staged at a time.
#:
#: Loading used to read the whole extract into one DataFrame, which peaked at
#: 4.0 GB on the full profile's 5,000,000 MES events -- almost exactly the
#: 4.3 GB that ADR-024 cites for holding them as dicts. Generation streams to
#: avoid that; the loader then undid the whole benefit one step downstream, and
#: BTB-36 asks for loading to be chunked as well.
#:
#: 100,000 rows is large enough that per-chunk overhead and round trips are
#: noise, and small enough that peak memory is a function of the chunk rather
#: than the file.
CHUNK_ROWS = 100_000

#: Synthetic column on the staging table holding each row's line number in the
#: source file.
#:
#: Needed because in-file duplicate detection moved from pandas to SQL. A
#: duplicate can straddle a chunk boundary, so a per-chunk check would miss
#: exactly the fault CTRL-001 depends on -- and the surplus has to be
#: identified as "not the first occurrence", which needs an ordering the
#: staging table did not otherwise have. It also keeps the reject log's row
#: numbers true to the file.
SOURCE_ROW_COLUMN = "_source_row"


def _validate(
    extract_name: str, frame: pd.DataFrame, row_offset: int = 0
) -> tuple[pd.DataFrame, list[RejectedRow]]:
    """Validate, returning the surviving rows and the quarantined ones.

    Args:
        extract_name: Which schema to validate against.
        frame: The rows to validate -- one chunk of the file.
        row_offset: How many rows of the file precede this chunk, so a
            rejected row's reported line number is its line in the FILE. A
            per-chunk index would restart at every chunk and send a reader to
            the wrong line.
    """
    schema = schemas.schema_for(extract_name)
    try:
        return schema.validate(frame, lazy=True), []
    except pa_errors.SchemaErrors as errors:
        failures = errors.failure_cases
        # `index` is the positional row in the frame; null for schema-level
        # failures such as a missing column, which are not row faults.
        bad_indices = sorted({int(i) for i in failures["index"].dropna().unique()})

        reasons: dict[int, list[str]] = {}
        for _, failure in failures.iterrows():
            if pd.isna(failure["index"]):
                continue
            reasons.setdefault(int(failure["index"]), []).append(
                f"{failure['column']}: {failure['check']} (got {failure['failure_case']!r})"
            )

        rejected = [
            RejectedRow(
                # +2: one for the header, one because files are 1-based.
                source_row=row_offset + index + 2,
                rule=REJECT_SCHEMA_VIOLATION,
                detail="; ".join(reasons.get(index, ["schema violation"])),
                raw_row={k: (None if pd.isna(v) else str(v)) for k, v in frame.iloc[index].items()},
            )
            for index in bad_indices
        ]

        surviving = frame.drop(index=frame.index[bad_indices])
        if surviving.empty:
            return surviving, rejected
        # Re-validate the survivors so the returned frame is coerced. Any
        # remaining failure is schema-level, not row-level, and must surface.
        return schema.validate(surviving, lazy=True), rejected


def _create_staging_table(
    cursor: psycopg.Cursor, extract_name: str, staging: sql.Identifier
) -> None:
    """A per-batch copy of the target's shape.

    UNLOGGED because the contents are transient -- it exists only long enough
    for the anti-join and is dropped in a `finally`. Skipping WAL for it is
    free performance on the full profile.
    """
    cursor.execute(
        sql.SQL("CREATE UNLOGGED TABLE {}.{} (LIKE {}.{} INCLUDING DEFAULTS)").format(
            sql.Identifier(RAW_SCHEMA),
            staging,
            sql.Identifier(RAW_SCHEMA),
            sql.Identifier(extract_name),
        )
    )
    # The source row number, which the target does not carry. Used to find the
    # FIRST occurrence of a duplicated natural key across the whole staged
    # file, and to report a rejected row's true line number.
    cursor.execute(
        sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} bigint").format(
            sql.Identifier(RAW_SCHEMA), staging, sql.Identifier(SOURCE_ROW_COLUMN)
        )
    )


def _copy_into_staging(
    cursor: psycopg.Cursor,
    staging: sql.Identifier,
    columns: Sequence[str],
    rows: Sequence[tuple[Any, ...]],
) -> None:
    statement = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        sql.Identifier(RAW_SCHEMA),
        staging,
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    with cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def _reject_staged_duplicates(
    cursor: psycopg.Cursor,
    extract_name: str,
    staging: sql.Identifier,
    business_columns: Sequence[str],
) -> list[RejectedRow]:
    """Quarantine staged rows duplicating their natural key within this file.

    The distinction the duplicate-receipt control depends on. A row already in
    `raw` from an earlier run is a rerun and is *skipped*. A row appearing
    twice in the same file means the upstream interface posted twice and is
    *rejected*. Both would look identical to a loader that only counted
    inserts (BTB-76, ADR-020).

    Done in SQL across the whole staged file rather than per chunk in pandas.
    A duplicate can straddle a chunk boundary, and a per-chunk check would miss
    it -- which would silently turn a rejected duplicate into an inserted one
    and leave CTRL-001 with nothing to find. Doing it here also costs no memory
    at all, which is the point of chunking in the first place.

    The first occurrence is kept, so the legitimate posting still loads and
    only the surplus is quarantined.
    """
    key = list(schemas.natural_key_for(extract_name))
    key_columns = sql.SQL(", ").join(sql.Identifier(column) for column in key)
    returning = sql.SQL(", ").join(
        sql.Identifier(column) for column in [*business_columns, SOURCE_ROW_COLUMN]
    )

    cursor.execute(
        sql.SQL(
            "DELETE FROM {raw}.{staging} "
            "WHERE {source_row} NOT IN ("
            "    SELECT min({source_row}) FROM {raw}.{staging} GROUP BY {key}"
            ") "
            "RETURNING {returning}"
        ).format(
            raw=sql.Identifier(RAW_SCHEMA),
            staging=staging,
            source_row=sql.Identifier(SOURCE_ROW_COLUMN),
            key=key_columns,
            returning=returning,
        )
    )

    rejected: list[RejectedRow] = []
    for row in cursor.fetchall():
        values = dict(zip([*business_columns, SOURCE_ROW_COLUMN], row, strict=True))
        source_row = values.pop(SOURCE_ROW_COLUMN)
        rejected.append(
            RejectedRow(
                source_row=int(source_row),
                rule=REJECT_DUPLICATE_NATURAL_KEY,
                detail=(
                    f"natural key {tuple(key)} = "
                    f"{tuple(str(values[column]) for column in key)} "
                    "appears more than once in this file"
                ),
                raw_row={k: (None if v is None else str(v)) for k, v in values.items()},
            )
        )
    return rejected


def _insert_new_rows(
    cursor: psycopg.Cursor,
    extract_name: str,
    staging: sql.Identifier,
    columns: Sequence[str],
) -> int:
    """Insert staged rows absent from the target. Returns how many landed.

    The anti-join is what makes the skip count a real, reportable number rather
    than a side effect of `ON CONFLICT` (ADR-020).
    """
    key = schemas.natural_key_for(extract_name)
    target = sql.SQL("{}.{}").format(sql.Identifier(RAW_SCHEMA), sql.Identifier(extract_name))
    column_list = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
    staged_columns = sql.SQL(", ").join(
        sql.SQL("s.{}").format(sql.Identifier(column)) for column in columns
    )
    join_predicate = sql.SQL(" AND ").join(
        sql.SQL("t.{column} = s.{column}").format(column=sql.Identifier(column)) for column in key
    )

    cursor.execute(
        sql.SQL(
            "INSERT INTO {target} ({columns}) "
            "SELECT {staged} FROM {raw}.{staging} s "
            "LEFT JOIN {target} t ON {predicate} "
            # Every key column is NOT NULL in the target, so a NULL here means
            # the left join found nothing -- the row is genuinely new.
            "WHERE t.{first_key} IS NULL"
        ).format(
            target=target,
            columns=column_list,
            staged=staged_columns,
            raw=sql.Identifier(RAW_SCHEMA),
            staging=staging,
            predicate=join_predicate,
            first_key=sql.Identifier(key[0]),
        )
    )
    return cursor.rowcount


def _record_rejects(
    cursor: psycopg.Cursor,
    run_id: uuid.UUID,
    batch_id: str,
    extract_name: str,
    source_file: str,
    rejects: Sequence[RejectedRow],
) -> None:
    if not rejects:
        return
    cursor.executemany(
        "INSERT INTO audit.rejected_rows "
        "(run_id, batch_id, extract_name, source_file, source_row, "
        " rejection_rule, rejection_detail, raw_row) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (
                run_id,
                batch_id,
                extract_name,
                source_file,
                reject.source_row,
                reject.rule,
                reject.detail,
                json.dumps(reject.raw_row, sort_keys=True),
            )
            for reject in rejects
        ],
    )


def _record_run(cursor: psycopg.Cursor, result: LoadResult) -> None:
    cursor.execute(
        "INSERT INTO audit.pipeline_runs "
        "(run_id, batch_id, extract_name, source_file, file_sha256, git_commit_sha, "
        " rows_in_file, rows_inserted, rows_skipped, rows_rejected, "
        " control_total_rows, control_total_value, started_at, ended_at, status, failure_reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            result.run_id,
            result.batch_id,
            result.extract_name,
            result.source_file,
            result.file_sha256,
            result.git_commit_sha,
            result.rows_in_file,
            result.rows_inserted,
            result.rows_skipped,
            result.rows_rejected,
            result.control_total_rows,
            result.control_total_value,
            result.started_at,
            result.ended_at,
            result.status,
            result.failure_reason,
        ),
    )


def load_extract(
    conn: psycopg.Connection,
    extract_name: str,
    csv_path: pathlib.Path,
    batch_id: str,
    run_id: uuid.UUID | None = None,
    commit_sha: str | None = None,
    control_totals_suffix: str = ".control_totals.json",
) -> LoadResult:
    """Validate and incrementally load one extract.

    Args:
        conn: Connection. Managed here, so it must not be in autocommit.
        extract_name: Raw table and schema name.
        csv_path: The extract file.
        batch_id: Shared by every file in one run.
        run_id: Defaults to a fresh UUID.
        commit_sha: Defaults to the running commit.
        control_totals_suffix: Sidecar naming (ADR-022).

    Returns:
        What the load did, already recorded in `audit.pipeline_runs`.

    Raises:
        FileNotFoundError: The sidecar is missing.
        ValueError: The sidecar's natural key disagrees with the loader's.
        Exception: Any load failure, re-raised *after* the failure is recorded
            in the audit trail.
    """
    run_id = run_id or uuid.uuid4()
    commit_sha = commit_sha or git_commit_sha()
    started_at = dt.datetime.now(dt.UTC)

    control_totals = _read_control_totals(csv_path, control_totals_suffix)

    declared_key = tuple(control_totals.get("natural_key", ()))
    expected_key = schemas.natural_key_for(extract_name)
    if declared_key != expected_key:
        raise ValueError(
            f"{extract_name}: the sidecar declares natural key {declared_key} but the "
            f"loader expects {expected_key}. One is wrong, and the anti-join's "
            f"correctness depends on the key -- refusing to guess (ADR-020)."
        )

    # Streams the file; never holds it. The digest has always been computed
    # this way, and as of BTB-36 so is everything else below.
    digest = file_sha256(csv_path)

    control_total_value = control_totals.get("value_total")
    value_column = control_totals.get("value_column")

    # Totals accumulated across chunks rather than taken from a whole frame.
    # The value total covers every parsed row INCLUDING any later rejected,
    # because the trailer describes the file as it arrived rather than what
    # survived validation.
    rows_in_file = 0
    value_total_in_file: decimal.Decimal | None = None

    result = LoadResult(
        run_id=run_id,
        batch_id=batch_id,
        extract_name=extract_name,
        source_file=csv_path.name,
        file_sha256=digest,
        git_commit_sha=commit_sha,
        rows_in_file=rows_in_file,
        rows_inserted=0,
        rows_skipped=0,
        rows_rejected=0,
        control_total_rows=control_totals.get("row_count"),
        control_total_value=(
            decimal.Decimal(control_total_value) if control_total_value is not None else None
        ),
        started_at=started_at,
        value_total_in_file=value_total_in_file,
    )

    staging = _staging_table(extract_name, batch_id)
    loaded_at = dt.datetime.now(dt.UTC)

    try:
        rejects: list[RejectedRow] = []
        business_columns = list(schemas.schema_for(extract_name).columns)
        provenance = {
            BATCH_ID_COLUMN: batch_id,
            SOURCE_FILE_COLUMN: csv_path.name,
            FILE_SHA256_COLUMN: digest,
            LOADED_AT_COLUMN: loaded_at,
        }
        all_columns = [*business_columns, *provenance]
        staging_columns = [*all_columns, SOURCE_ROW_COLUMN]

        with conn.transaction(), conn.cursor() as cursor:
            _create_staging_table(cursor, extract_name, staging)

            staged = 0
            # One chunk at a time: read, total, validate, stage. Peak memory is
            # a function of CHUNK_ROWS rather than of the file, which is what
            # BTB-36 asks for and what the whole-frame read defeated.
            for raw_chunk in pd.read_csv(
                csv_path, dtype=str, keep_default_na=True, chunksize=CHUNK_ROWS
            ):
                row_offset = rows_in_file
                rows_in_file += len(raw_chunk)

                if value_column and value_column in raw_chunk.columns:
                    if value_total_in_file is None:
                        value_total_in_file = decimal.Decimal("0")
                    value_total_in_file += sum(
                        (
                            decimal.Decimal(str(value))
                            for value in raw_chunk[value_column]
                            if value is not None and str(value) not in ("", "nan")
                        ),
                        decimal.Decimal("0"),
                    )

                valid_frame, schema_rejects = _validate(extract_name, raw_chunk, row_offset)
                rejects.extend(schema_rejects)
                if valid_frame.empty:
                    continue

                frame = valid_frame.copy()
                for column, value in provenance.items():
                    frame[column] = value
                # The row's line in the file: +2 for the header and 1-based
                # counting, matching how a rejected row is reported.
                frame[SOURCE_ROW_COLUMN] = [
                    row_offset + position + 2 for position in range(len(frame))
                ]
                rows = _to_db_rows(frame, staging_columns)
                _copy_into_staging(cursor, staging, staging_columns, rows)
                staged += len(rows)

            # Across the whole staged file, not per chunk: a duplicate can
            # straddle a chunk boundary.
            if staged:
                duplicate_rejects = _reject_staged_duplicates(
                    cursor, extract_name, staging, business_columns
                )
                rejects.extend(duplicate_rejects)
                staged -= len(duplicate_rejects)

            result.rows_rejected = len(rejects)
            result.rows_in_file = rows_in_file
            result.value_total_in_file = value_total_in_file

            inserted = _insert_new_rows(cursor, extract_name, staging, all_columns) if staged else 0
            result.rows_inserted = inserted
            result.rows_skipped = staged - inserted

            _record_rejects(cursor, run_id, batch_id, extract_name, csv_path.name, rejects)

        result.status = "success"
    except Exception as error:
        result.status = "failed"
        result.failure_reason = f"{type(error).__name__}: {error}"[:2000]
        raise
    finally:
        result.ended_at = dt.datetime.now(dt.UTC)
        # The staging table is dropped on both paths, or `raw` accumulates
        # litter from every failed run (ADR-020).
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(RAW_SCHEMA), staging)
            )
        # The audit row is written even when the load failed: a run that left
        # no trace is the one an auditor cannot test.
        with conn.transaction(), conn.cursor() as cursor:
            _record_run(cursor, result)

    return result
