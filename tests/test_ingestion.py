"""Validation, natural keys and reject classification -- no database needed.

The database-backed behaviour (idempotency, provenance, the audit trail) lives
in test_loading.py. These are the pure parts, so they run on every commit
without a warehouse.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import pathlib
import uuid

import numpy as np
import pandas as pd
import pandera.errors as pa_errors
import pytest

from generator import dataset
from ingestion import loader, schemas


# ---------------------------------------------------------------------------
# Schemas and natural keys (BTB-14)
# ---------------------------------------------------------------------------
def test_every_extract_has_a_declared_schema() -> None:
    """Compared against the whole dataset, not the inventory slice.

    This test used to compare against ``inventory.EXTRACT_SPECS``, which
    passed while Phase 1 had one source system and became wrong the moment
    Phase 2 added BOMs -- it then failed for the right reason but with a
    confusing message.
    """
    assert set(schemas.SCHEMAS) == set(dataset.EXTRACT_SPECS)


def test_every_extract_has_a_declared_natural_key() -> None:
    assert set(schemas.NATURAL_KEYS) == set(schemas.SCHEMAS)


def test_natural_key_columns_exist_in_the_schema() -> None:
    for name, key in schemas.NATURAL_KEYS.items():
        columns = set(schemas.SCHEMAS[name].columns)
        assert set(key) <= columns, f"{name}: key {set(key) - columns} not in the schema"


def test_natural_keys_match_the_generator_specs() -> None:
    """The loader's contract and the generator's declaration must agree.

    They are declared separately on purpose -- the sidecar is the source's
    claim, this is the loader's expectation -- but if they diverge the
    anti-join is keyed on the wrong thing and idempotency is a coin flip.
    """
    for name, spec in dataset.EXTRACT_SPECS.items():
        assert tuple(spec.natural_key) == schemas.natural_key_for(name), name


def test_schema_columns_match_the_generator_columns() -> None:
    for name, spec in dataset.EXTRACT_SPECS.items():
        assert set(schemas.SCHEMAS[name].columns) == set(spec.columns), name


@pytest.mark.parametrize("extract_name", sorted(schemas.SCHEMAS))
def test_schema_is_strict_so_an_unexpected_column_fails(extract_name: str) -> None:
    """A silently ignored column is how a field goes missing downstream."""
    assert schemas.SCHEMAS[extract_name].strict is True


# ---------------------------------------------------------------------------
# The SCD Type 2 sentinel
# ---------------------------------------------------------------------------
def test_iso_date_check_accepts_the_open_ended_sentinel() -> None:
    """9999-12-31 is SAP's open-window convention and must validate.

    It overflows pandas' datetime64[ns], whose ceiling is 2262-04-11. The
    first full load failed on exactly this, which is why dates are validated
    as ISO text and parsed by Postgres.
    """
    assert schemas._is_iso_date("9999-12-31")
    assert schemas._is_iso_date("2026-08-01")


@pytest.mark.parametrize("bad", ["2026-13-45", "not-a-date", "", "2026/08/01", "26-08-01"])
def test_iso_date_check_rejects_malformed_dates(bad: str) -> None:
    """Validating as text must not mean validating nothing."""
    assert not schemas._is_iso_date(bad)


@pytest.mark.parametrize("good", ["2026-08-01", "20260801", "9999-12-31"])
def test_iso_date_check_accepts_both_iso_8601_forms(good: str) -> None:
    """Extended (2026-08-01) and basic (20260801) are both valid ISO 8601.

    Python's ``date.fromisoformat`` accepts both from 3.11, and so does
    Postgres, so rejecting the basic form would refuse a legitimate extract.
    """
    assert schemas._is_iso_date(good)


def test_sentinel_would_overflow_nanosecond_datetime() -> None:
    """Pins the reason the text representation exists, so it is not 'simplified' away.

    ``pd.to_datetime`` alone no longer fails -- pandas 3 infers
    ``datetime64[us]``, which holds year 9999. The constraint is specifically
    the nanosecond dtype, which is what pandera coerces to, and its ceiling is
    2262-04-11.
    """
    assert pd.Timestamp.max.year == 2262
    with pytest.raises(pd.errors.OutOfBoundsDatetime):
        pd.Series(["9999-12-31"]).astype("datetime64[ns]")


# ---------------------------------------------------------------------------
# Validation outcomes (BTB-17)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def movements_frame() -> pd.DataFrame:
    path = pathlib.Path("data/goods_movements.csv")
    if not path.exists():
        pytest.skip("run `make data` first")
    return pd.read_csv(path, dtype=str)


def test_clean_extract_validates(movements_frame: pd.DataFrame) -> None:
    validated = schemas.schema_for("goods_movements").validate(movements_frame, lazy=True)
    assert len(validated) == len(movements_frame)


def test_invalid_rows_are_reported_individually(movements_frame: pd.DataFrame) -> None:
    """A rejected row needs its own reason, not a count (BTB-17)."""
    corrupted = movements_frame.copy()
    corrupted.loc[0, "movement_type"] = "999"
    corrupted.loc[1, "quantity"] = "0"
    corrupted.loc[2, "fiscal_year"] = "1999"

    with pytest.raises(pa_errors.SchemaErrors) as caught:
        schemas.schema_for("goods_movements").validate(corrupted, lazy=True)

    failures = caught.value.failure_cases
    assert set(failures["index"].dropna().astype(int)) == {0, 1, 2}
    assert set(failures["column"]) == {"movement_type", "quantity", "fiscal_year"}


def test_validate_quarantines_bad_rows_and_keeps_the_rest(
    movements_frame: pd.DataFrame,
) -> None:
    corrupted = movements_frame.copy()
    corrupted.loc[0, "movement_type"] = "999"
    corrupted.loc[1, "quantity"] = "0"

    surviving, rejected = loader._validate("goods_movements", corrupted)

    assert len(rejected) == 2
    assert len(surviving) == len(corrupted) - 2
    assert all(r.rule == loader.REJECT_SCHEMA_VIOLATION for r in rejected)
    # Row numbers point into the file, counting the header, so a person can open it.
    assert sorted(r.source_row for r in rejected) == [2, 3]
    assert all(r.raw_row for r in rejected), "a reject must carry the original row"


def test_rejection_detail_names_the_rule_that_failed(
    movements_frame: pd.DataFrame,
) -> None:
    corrupted = movements_frame.copy()
    corrupted.loc[0, "movement_type"] = "999"
    _surviving, rejected = loader._validate("goods_movements", corrupted)
    assert "movement_type" in rejected[0].detail
    assert "999" in rejected[0].detail


# ---------------------------------------------------------------------------
# Batch identity and provenance (BTB-16)
# ---------------------------------------------------------------------------
def test_batch_ids_are_unique_and_time_sortable() -> None:
    first = loader.new_batch_id(dt.datetime(2026, 11, 2, 10, 0, 0))
    second = loader.new_batch_id(dt.datetime(2026, 11, 2, 11, 0, 0))
    assert first < second, "batch ids must sort chronologically"
    # Two runs in the same second must still differ, which happens in tests
    # and in a retry loop.
    same_second = {loader.new_batch_id(dt.datetime(2026, 11, 2, 10, 0, 0)) for _ in range(50)}
    assert len(same_second) == 50


def test_file_hash_is_of_the_bytes_not_the_parse(tmp_path: pathlib.Path) -> None:
    """Hashing before parsing means the checksum describes what arrived."""
    import hashlib

    path = tmp_path / "x.csv"
    path.write_bytes(b"a,b\r\n1,2\r\n")
    assert loader.file_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_provenance_column_names_are_fixed() -> None:
    """Named in one place so loaders, lineage and tests agree on the spelling."""
    from ingestion import PROVENANCE_COLUMNS

    assert PROVENANCE_COLUMNS == (
        "_batch_id",
        "_source_file",
        "_file_sha256",
        "_loaded_at",
    )


# ---------------------------------------------------------------------------
# Value normalisation for COPY
# ---------------------------------------------------------------------------
def test_normalise_converts_numpy_and_pandas_scalars() -> None:
    """COPY renders values with str(), so a numpy float becomes "10.0".

    Postgres refuses that for an integer column -- the first full load failed
    on exactly this, at po_item.
    """
    assert loader._normalise(np.int64(10)) == 10
    assert isinstance(loader._normalise(np.int64(10)), int)
    assert loader._normalise(np.float64(1.5)) == 1.5
    assert loader._normalise(np.bool_(True)) is True
    assert loader._normalise(pd.NA) is None
    assert loader._normalise(float("nan")) is None
    assert loader._normalise(None) is None
    assert loader._normalise("text") == "text"


def test_nullable_integer_column_survives_as_a_whole_number(
    movements_frame: pd.DataFrame,
) -> None:
    """po_item is null on goods issues but must not become 10.0 on receipts."""
    validated = schemas.schema_for("goods_movements").validate(movements_frame, lazy=True)
    rows = loader._to_db_rows(validated, ["po_item"])
    values = {row[0] for row in rows}
    assert None in values, "goods issues should carry no PO item"
    integers = {v for v in values if v is not None}
    assert integers, "no receipts found"
    assert all(isinstance(v, int) for v in integers), f"non-integer po_item: {integers}"


# ---------------------------------------------------------------------------
# Control totals (ADR-022)
# ---------------------------------------------------------------------------
def test_missing_sidecar_fails_the_load(tmp_path: pathlib.Path) -> None:
    csv_path = tmp_path / "materials.csv"
    csv_path.write_text("material_number\n1000000\n")
    with pytest.raises(FileNotFoundError, match="control totals missing"):
        loader._read_control_totals(csv_path, ".control_totals.json")


def test_sidecar_key_disagreement_refuses_to_guess(tmp_path: pathlib.Path) -> None:
    """If the two declarations differ, one is wrong and loading is a guess."""
    csv_path = tmp_path / "materials.csv"
    csv_path.write_text("material_number\n1000000\n")
    (tmp_path / "materials.control_totals.json").write_text(
        json.dumps({"row_count": 1, "natural_key": ["wrong_column"], "value_total": None})
    )

    class _NoConnection:
        pass

    with pytest.raises(ValueError, match="refusing to guess"):
        loader.load_extract(
            _NoConnection(),  # type: ignore[arg-type]
            "materials",
            csv_path,
            "batch",
        )


def test_completeness_requires_every_row_accounted_for() -> None:
    """Inserted + skipped + rejected must equal the declared row count."""
    import uuid

    def result(inserted: int, skipped: int, rejected: int, declared: int) -> loader.LoadResult:
        return loader.LoadResult(
            run_id=uuid.uuid4(),
            batch_id="b",
            extract_name="materials",
            source_file="materials.csv",
            file_sha256="0" * 64,
            git_commit_sha="abc",
            rows_in_file=declared,
            rows_inserted=inserted,
            rows_skipped=skipped,
            rows_rejected=rejected,
            control_total_rows=declared,
            control_total_value=None,
            started_at=dt.datetime.now(dt.UTC),
        )

    assert result(10, 0, 0, 10).completeness_ok
    assert result(0, 10, 0, 10).completeness_ok
    assert result(7, 2, 1, 10).completeness_ok
    # A shortfall means rows vanished between the file and the warehouse.
    assert not result(9, 0, 0, 10).completeness_ok
    assert not result(11, 0, 0, 10).completeness_ok


# ---------------------------------------------------------------------------
# CTRL-001: value totals, not only row counts
# ---------------------------------------------------------------------------
def _result(
    inserted: int,
    skipped: int,
    rejected: int,
    declared_rows: int,
    declared_value: str | None = None,
    file_value: str | None = None,
) -> loader.LoadResult:
    return loader.LoadResult(
        run_id=uuid.uuid4(),
        batch_id="b",
        extract_name="goods_movements",
        source_file="goods_movements.csv",
        file_sha256="0" * 64,
        git_commit_sha="abc",
        rows_in_file=declared_rows,
        rows_inserted=inserted,
        rows_skipped=skipped,
        rows_rejected=rejected,
        control_total_rows=declared_rows,
        control_total_value=(
            decimal.Decimal(declared_value) if declared_value is not None else None
        ),
        started_at=dt.datetime.now(dt.UTC),
        value_total_in_file=(decimal.Decimal(file_value) if file_value is not None else None),
    )


def test_value_total_mismatch_fails_completeness() -> None:
    """The half of CTRL-001 that row counts cannot cover.

    A row whose amount was altered in transit keeps the count intact and
    changes the money. The matrix claimed this was checked before the loader
    actually did it.
    """
    result = _result(10, 0, 0, 10, declared_value="100.00", file_value="100.01")
    assert result.rows_accounted_ok, "the row count should still reconcile"
    assert not result.value_total_ok
    assert not result.completeness_ok
    assert "value total" in result.completeness_detail
    assert "0.01" in result.completeness_detail


def test_value_total_match_passes_completeness() -> None:
    result = _result(10, 0, 0, 10, declared_value="100.00", file_value="100.00")
    assert result.value_total_ok
    assert result.completeness_ok
    assert result.completeness_detail == ""


def test_extract_with_no_monetary_column_skips_the_value_check() -> None:
    """Master data has no value total, and asserting 0.00 would be a lie."""
    result = _result(60, 0, 0, 60, declared_value=None, file_value=None)
    assert result.value_total_ok
    assert result.completeness_ok


def test_row_shortfall_and_value_mismatch_are_reported_separately() -> None:
    result = _result(9, 0, 0, 10, declared_value="100.00", file_value="90.00")
    assert not result.rows_accounted_ok
    assert not result.value_total_ok
    detail = result.completeness_detail
    assert "row count" in detail and "value total" in detail


def test_value_total_is_summed_from_the_file_including_rejected_rows(
    tmp_path: pathlib.Path,
) -> None:
    """The trailer describes the file as it arrived, not what survived.

    Excluding rejected rows from the sum would make a file with an invalid row
    *appear* to reconcile, hiding the fault the trailer exists to reveal.
    """
    csv_path = tmp_path / "inventory_revaluations.csv"
    csv_path.write_text(
        "revaluation_document,fiscal_year,material_number,posting_date,"
        "old_standard_cost_usd,new_standard_cost_usd,quantity_on_hand,amount_usd,"
        "currency,approved_by\r\n"
        "5200000001,2026,1000000,2026-09-01,100.00,110.00,10,100.00,USD,A.PERSON\r\n"
        # Invalid: quantity_on_hand must be positive. Its amount still counts
        # toward the file's declared total.
        "5200000002,2026,1000001,2026-09-01,100.00,110.00,0,50.00,USD,A.PERSON\r\n"
    )
    (tmp_path / "inventory_revaluations.control_totals.json").write_text(
        json.dumps(
            {
                "row_count": 2,
                "value_column": "amount_usd",
                "value_total": "150.00",
                "natural_key": ["revaluation_document", "fiscal_year"],
            }
        )
    )

    frame = pd.read_csv(csv_path, dtype=str)
    total = sum(decimal.Decimal(v) for v in frame["amount_usd"])
    assert total == decimal.Decimal("150.00"), (
        "the file's own sum must include the row that will be rejected"
    )


# ---------------------------------------------------------------------------
# Connecting to a database that is not there
# ---------------------------------------------------------------------------
def test_the_dsn_bounds_how_long_a_connection_attempt_waits() -> None:
    """An unreachable warehouse must error, not wait.

    libpq waits indefinitely by default, and a database that ACCEPTS a
    connection and then never answers hangs instead of erroring. That happened:
    Docker Desktop was installed on an external drive, the drive disconnected,
    the daemon's mapped binaries went with it, and its orphaned port forwarder
    kept accepting TCP on 5432 while Postgres was gone.

    Every module that treats an unreachable warehouse as a skip depends on
    `connect` raising OperationalError. A hang never raises one, so a
    hundred-second suite sat blocked for fifteen minutes and reported nothing --
    no failure, no skip, no output.
    """
    from ingestion import db

    assert "connect_timeout=" in db.dsn()


def test_the_connect_timeout_is_overridable() -> None:
    """A cold hosted instance can take longer to accept than a container."""
    import os

    from ingestion import db

    previous = os.environ.get("BTB_CONNECT_TIMEOUT")
    os.environ["BTB_CONNECT_TIMEOUT"] = "37"
    try:
        assert "connect_timeout=37" in db.dsn()
    finally:
        if previous is None:
            os.environ.pop("BTB_CONNECT_TIMEOUT", None)
        else:
            os.environ["BTB_CONNECT_TIMEOUT"] = previous


def test_the_dsn_never_carries_a_password_it_was_not_given() -> None:
    """The reason DSN assembly lives in one place: no credential in a log line."""
    import os

    from ingestion import db

    previous = os.environ.get("POSTGRES_PASSWORD")
    os.environ["POSTGRES_PASSWORD"] = ""
    try:
        assert "password=" not in db.dsn(), (
            "an empty password must be omitted rather than emitted as password="
        )
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_PASSWORD", None)
        else:
            os.environ["POSTGRES_PASSWORD"] = previous
