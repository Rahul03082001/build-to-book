"""Validated, incremental, idempotent loaders.

Every extract is validated against a declared schema before it lands, bad rows
are rejected and logged rather than dropped, and each landed row carries its
provenance: `_batch_id`, `_source_file`, `_file_sha256`, `_loaded_at`.

Loading is incremental on source IDs (ADR-017), so a rerun after a failure
adds nothing twice -- the property that keeps a retry from double-counting
inventory. Phase 1 implements the inventory loaders (BTB-14..BTB-17).
"""

# Provenance columns stamped on every landed row. Named here so the loaders,
# the lineage tracer, and the tests agree on one spelling.
BATCH_ID_COLUMN = "_batch_id"
SOURCE_FILE_COLUMN = "_source_file"
FILE_SHA256_COLUMN = "_file_sha256"
LOADED_AT_COLUMN = "_loaded_at"

PROVENANCE_COLUMNS = (
    BATCH_ID_COLUMN,
    SOURCE_FILE_COLUMN,
    FILE_SHA256_COLUMN,
    LOADED_AT_COLUMN,
)
