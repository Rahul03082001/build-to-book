"""Shared fixtures and warehouse helpers.

`REPO_ROOT` is resolved from this file's location rather than the working
directory, so the suite behaves the same from the repo root, from an IDE, and
in CI.

The warehouse helpers live here because two integration modules need them and,
more importantly, because **a module that depends on warehouse state must
establish that state itself**. Inheriting "whatever ran last" caused a real
failure: the publish-gate tests took the most recent successful load as their
batch, and the module running just before them ends with a load that
deliberately contains rejected rows. The gate then failed CTRL-001 on another
module's test fixture. Alone it passed; in a full run it did not.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_order() -> tuple[str, ...]:
    """The extract order, from the generator -- never a second copy."""
    from generator import dataset

    return dataset.LOAD_ORDER


#: Masters before the documents that reference them.
LOAD_ORDER = _load_order()

#: Fixed timestamp, so sidecars are reproducible across runs.
EXTRACT_STAMP = dt.datetime(2026, 11, 2, 12, 0, 0)


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    return REPO_ROOT


# ---------------------------------------------------------------------------
# Warehouse helpers
# ---------------------------------------------------------------------------
def generate_extracts(out_dir: pathlib.Path, defect_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """Generate the demo extracts, planting the given defects. Returns the manifest."""
    from generator import config as genconfig
    from generator import dataset, defects

    cfg = genconfig.load()
    # Built through dataset.build, not InventoryGenerator directly: this helper
    # must produce every extract the loader expects. Generating only the
    # inventory slice left the BOM extracts missing and the loader failed with
    # "control totals missing for bom_headers.csv" -- a confusing symptom of a
    # test helper that had not kept up with a new source system.
    data, planted = dataset.build(cfg, "demo", defect_keys)

    manifest = defects.DefectManifest(seed=cfg.seed, profile="demo", generated_at=EXTRACT_STAMP)
    for record in planted:
        manifest.add(record)

    # dataset.write_all, not a local loop over as_mapping(): streamed and
    # deferred extracts need the dispatch the CLI uses, and a helper with its
    # own copy of it falls behind the moment a source changes shape.
    dataset.write_all(data, out_dir, EXTRACT_STAMP)
    manifest.write(out_dir / "defects_manifest.json")
    return manifest.to_dict()


def reset_raw(conn: psycopg.Connection) -> None:
    """Empty the raw tables so insert counts are meaningful."""
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("TRUNCATE " + ", ".join(f"raw.{name}" for name in LOAD_ORDER))


def load_all(conn: psycopg.Connection, data_dir: pathlib.Path) -> dict[str, Any]:
    """Load every extract under one batch. Returns the batch id and results."""
    from ingestion import loader

    batch_id = loader.new_batch_id()
    results = {
        name: loader.load_extract(conn, name, data_dir / f"{name}.csv", batch_id)
        for name in LOAD_ORDER
    }
    return {"batch_id": batch_id, "results": results}


def dbt_build(*, skip_on_failure: bool = True) -> subprocess.CompletedProcess[str]:
    """Rebuild the warehouse.

    Args:
        skip_on_failure: Skip the test when dbt is unavailable or fails. False
            in teardown, where skipping would mask the real test result.
    """
    result = subprocess.run(
        [
            str(REPO_ROOT / ".venv" / "bin" / "dbt"),
            "build",
            "--project-dir",
            str(REPO_ROOT / "warehouse" / "dbt"),
            "--profiles-dir",
            str(REPO_ROOT / "warehouse" / "dbt"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if skip_on_failure and result.returncode != 0:
        pytest.skip(f"dbt build unavailable: {result.stdout[-400:]}")
    return result


def establish_clean_warehouse(conn: psycopg.Connection) -> str:
    """Load and build a known-clean dataset. Returns its batch id.

    Any module asserting on control results needs a batch it put there itself.
    Taking the most recent load instead means another module's deliberately
    broken fixture becomes your test's input.
    """
    with tempfile.TemporaryDirectory() as directory:
        clean = pathlib.Path(directory)
        generate_extracts(clean)
        reset_raw(conn)
        loaded = load_all(conn, clean)
    dbt_build()
    return loaded["batch_id"]


@pytest.fixture(scope="module")
def warehouse() -> Iterator[psycopg.Connection]:
    """A connection, or a skip when no warehouse is reachable."""
    from ingestion import db

    try:
        connection = db.connect()
    except psycopg.OperationalError as error:
        pytest.skip(f"warehouse unavailable: {error}")
    with connection:
        yield connection
