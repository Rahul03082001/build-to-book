"""The publish gate (BTB-23, ADR-010, ADR-014, ADR-021).

Nothing reaches `reporting` unless every critical check passed. When one
fails, `reporting` keeps the dataset it already had, and `audit.publish_log`
records which checks blocked it and which version is still being served -- so
"why is the dashboard showing yesterday?" has an answer that does not require
reading logs.

How the swap works:

1. Each published mart is copied from the build schema into `reporting` as a
   **versioned table**: ``mart_inventory_balance_v20261102T201906_aa7f84c9``.
2. One transaction recreates a stable **view** per mart over the new version.
3. Older versions are pruned, keeping the current and previous ones.

Why views over versioned tables, and not a schema rename: ``ALTER DEFAULT
PRIVILEGES`` is keyed to the schema *name*, so renaming `publish_candidate` to
`reporting` would silently strip the `reporter` role's SELECT from every table
the next build creates. The live dashboard would break one run later with
nothing in the logs pointing at the rename (ADR-021).

The swap is a catalogue change inside one transaction, so a reader sees the old
version or the new one and never a mixture -- and it never takes an exclusive
lock on a large table, which is what keeps blocking a bad publish cheap rather
than an outage.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import uuid

import psycopg
from psycopg import sql

from controls.runner import RunSummary

#: Marts published to `reporting`. Explicit rather than inferred, so adding a
#: model to the certified dataset is a deliberate, reviewable act.
#:
#: Phase 5 drives the lineage report from dbt's `meta: {certified: true}` tags
#: (ADR-009); this list should be generated from the same manifest then, so the
#: two cannot disagree about what "certified" means.
PUBLISHED_MARTS: tuple[str, ...] = (
    "mart_inventory_balance",
    "dim_material_current",
)

#: Build schema the gate copies from.
BUILD_SCHEMA = "marts"

#: Versions of each mart kept in `reporting`: the one being served and the one
#: before it. Two copies of every mart is already most of a 1 GB free tier
#: (ADR-015, ADR-016), so anything older is pruned.
VERSIONS_RETAINED = 2

STATUS_PUBLISHED = "published"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

_IDENTIFIER_SAFE = re.compile(r"[^0-9a-zA-Z_]+")


class PublishError(RuntimeError):
    """Raised when the swap itself fails, as opposed to being blocked."""


def version_suffix(published_at: dt.datetime, publish_id: uuid.UUID) -> str:
    """An identifier-safe version suffix, unique per publish **attempt**.

    Versioned by attempt, not by batch. ADR-021 originally named versions after
    the batch, which breaks on a republish: the versioned table is the one the
    live view depends on, so copying over it fails with
    `DependentObjectsStillExist`. Republishing is not exotic -- the nightly job
    retries, and `make publish` gets rerun by hand.

    Dropping the view first would resolve the dependency, but would hold a lock
    on it for the duration of the copy, stalling every reader of the live
    dataset precisely while a publish is in flight. A fresh table per attempt
    avoids the conflict entirely, and `audit.publish_log` keeps the
    batch-to-version mapping -- so traceability lives in the audit trail rather
    than in a table name, which is where it belongs anyway.

    The timestamp prefix sorts chronologically; the id suffix separates two
    attempts started in the same second.

    Postgres truncates identifiers at 63 bytes, and a silently truncated name
    would make two versions collide, so the suffix is bounded here rather than
    left to the server.
    """
    stamp = published_at.strftime("%Y%m%dT%H%M%S")
    return f"v{stamp}_{publish_id.hex[:6]}"


@dataclasses.dataclass
class PublishResult:
    """What a publish attempt did."""

    publish_id: uuid.UUID
    run_id: uuid.UUID
    batch_id: str
    target_schema: str
    candidate_version: str
    published_version: str | None
    previous_version: str | None
    status: str
    blocking_checks: list[str]
    checks_run: int
    checks_passed: int
    reason: str | None
    started_at: dt.datetime
    ended_at: dt.datetime

    @property
    def published(self) -> bool:
        return self.status == STATUS_PUBLISHED


def _existing_versions(cursor: psycopg.Cursor, target_schema: str, mart: str) -> list[str]:
    """Version suffixes present for a mart, newest first.

    Ordered by name, which works because the suffix starts with a sortable
    timestamp.
    """
    cursor.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE %s "
        "ORDER BY table_name DESC",
        (target_schema, f"{mart}_v%"),
    )
    prefix = f"{mart}_"
    return [row[0][len(prefix) :] for row in cursor.fetchall()]


def _currently_served_version(cursor: psycopg.Cursor, target_schema: str, mart: str) -> str | None:
    """Which version the stable view currently points at.

    Read from the view's own definition rather than assumed from the table
    names, so a rollback that repointed the view is reported honestly.
    """
    cursor.execute(
        "SELECT pg_get_viewdef(c.oid) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'v'",
        (target_schema, mart),
    )
    row = cursor.fetchone()
    if not row:
        return None
    match = re.search(rf"{re.escape(mart)}_(v[0-9A-Za-z_]+)", row[0])
    return match.group(1) if match else None


def _copy_mart(cursor: psycopg.Cursor, target_schema: str, mart: str, version: str) -> None:
    versioned = sql.Identifier(f"{mart}_{version}")
    # The version is unique per attempt, so this only ever cleans up after a
    # crashed attempt. RESTRICT (the default) is deliberate: if a view somehow
    # depends on this table, failing is correct -- CASCADE would drop the live
    # view and take the dashboard down.
    cursor.execute(
        sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(target_schema), versioned)
    )
    cursor.execute(
        sql.SQL("CREATE TABLE {}.{} AS SELECT * FROM {}.{}").format(
            sql.Identifier(target_schema),
            versioned,
            sql.Identifier(BUILD_SCHEMA),
            sql.Identifier(mart),
        )
    )


def _point_view_at(cursor: psycopg.Cursor, target_schema: str, mart: str, version: str) -> None:
    """Recreate the stable view over a version.

    CREATE OR REPLACE would fail if the column list changed between versions,
    which is exactly when a publish matters most, so the view is dropped and
    recreated. Both statements run inside the caller's transaction, so no
    reader ever sees the view missing.
    """
    cursor.execute(
        sql.SQL("DROP VIEW IF EXISTS {}.{}").format(
            sql.Identifier(target_schema), sql.Identifier(mart)
        )
    )
    cursor.execute(
        sql.SQL("CREATE VIEW {}.{} AS SELECT * FROM {}.{}").format(
            sql.Identifier(target_schema),
            sql.Identifier(mart),
            sql.Identifier(target_schema),
            sql.Identifier(f"{mart}_{version}"),
        )
    )
    # The reporter role reads the views, never a versioned table directly --
    # which is what stops a consumer pinning itself to a version that will be
    # pruned out from under it.
    cursor.execute(
        sql.SQL("GRANT SELECT ON {}.{} TO btb_reporter").format(
            sql.Identifier(target_schema), sql.Identifier(mart)
        )
    )


def _prune_versions(
    cursor: psycopg.Cursor, target_schema: str, mart: str, keep: set[str]
) -> list[str]:
    """Drop versions outside the retained set. Never drops a served version."""
    dropped: list[str] = []
    for version in _existing_versions(cursor, target_schema, mart):
        if version in keep:
            continue
        cursor.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                sql.Identifier(target_schema), sql.Identifier(f"{mart}_{version}")
            )
        )
        dropped.append(version)
    return dropped


def _record(conn: psycopg.Connection, result: PublishResult, commit_sha: str) -> None:
    """Append the attempt to the publish log, in its own transaction.

    A blocked publish must leave its explanation behind, so this cannot share a
    transaction with work that rolls back.
    """
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO audit.publish_log "
            "(publish_id, run_id, batch_id, git_commit_sha, target_schema, "
            " candidate_version, published_version, previous_version, status, "
            " blocking_checks, checks_run, checks_passed, reason, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                result.publish_id,
                result.run_id,
                result.batch_id,
                commit_sha,
                result.target_schema,
                result.candidate_version,
                result.published_version,
                result.previous_version,
                result.status,
                result.blocking_checks,
                result.checks_run,
                result.checks_passed,
                result.reason,
                result.started_at,
                result.ended_at,
            ),
        )


def publish(
    conn: psycopg.Connection,
    summary: RunSummary,
    commit_sha: str,
    target_schema: str = "reporting",
    marts: tuple[str, ...] = PUBLISHED_MARTS,
) -> PublishResult:
    """Swap a validated build into `reporting`, or block and leave it alone.

    Args:
        conn: Connection. Transactions are managed here.
        summary: Results from `controls.runner.run_checks`.
        commit_sha: The commit that produced the build.
        target_schema: Where to publish.
        marts: Which marts to publish.

    Returns:
        What happened, already recorded in `audit.publish_log`.

    Raises:
        PublishError: The swap itself failed. `reporting` is left on its
            previous version and the failure is recorded first.
    """
    started = dt.datetime.now(dt.UTC)
    publish_id = uuid.uuid4()
    candidate_version = version_suffix(started, publish_id)

    # Wrapped in a transaction even though it only reads. On a connection with
    # autocommit off, a bare cursor read opens a transaction that nothing ever
    # commits, and psycopg then nests every later `conn.transaction()` as a
    # savepoint inside it -- so the read's ACCESS SHARE locks are held until
    # the connection closes. A dbt run in the same test session blocked on
    # exactly that: `idle in transaction` here, `wait_event_type = Lock` there.
    with conn.transaction(), conn.cursor() as cursor:
        previous_version = _currently_served_version(cursor, target_schema, marts[0])

    def finish(status: str, published_version: str | None, reason: str | None) -> PublishResult:
        result = PublishResult(
            publish_id=publish_id,
            run_id=summary.run_id,
            batch_id=summary.batch_id,
            target_schema=target_schema,
            candidate_version=candidate_version,
            published_version=published_version,
            previous_version=previous_version,
            status=status,
            blocking_checks=summary.blocking_check_ids,
            checks_run=summary.checks_run,
            checks_passed=summary.checks_passed,
            reason=reason,
            started_at=started,
            ended_at=dt.datetime.now(dt.UTC),
        )
        _record(conn, result, commit_sha)
        return result

    # --- the gate -------------------------------------------------------
    if summary.blocking_failures:
        failures = "; ".join(
            f"{r.check.check_id} {r.check.name}" + (f" [{r.period}]" if r.period else "")
            for r in summary.blocking_failures
        )
        return finish(
            STATUS_BLOCKED,
            # Reporting keeps serving what it already had -- that is the point.
            published_version=previous_version,
            reason=(
                f"{len(summary.blocking_failures)} critical check(s) failed, so the "
                f"candidate was not published and {target_schema} still serves "
                f"{previous_version or 'no dataset'}: {failures}"
            )[:4000],
        )

    # --- the swap -------------------------------------------------------
    try:
        # Copies get their own transaction, separate from the swap. They are
        # the slow part, and the new tables are not referenced by any view
        # yet, so committing them early keeps the swap transaction short --
        # which is what stops a publish from stalling readers of the live
        # dataset.
        #
        # This uses `conn.transaction()` rather than an explicit
        # `conn.commit()`: psycopg forbids an explicit commit while a
        # transaction context is open anywhere up the stack, and the caller
        # may legitimately have one. CI caught that; a local smoke test on a
        # fresh connection did not.
        with conn.transaction(), conn.cursor() as cursor:
            for mart in marts:
                _copy_mart(cursor, target_schema, mart, candidate_version)

        with conn.transaction(), conn.cursor() as cursor:
            # One transaction for every view, so the whole certified dataset
            # moves together. A reader sees the old version or the new one.
            for mart in marts:
                _point_view_at(cursor, target_schema, mart, candidate_version)

        with conn.transaction(), conn.cursor() as cursor:
            # Exactly the version now being served and the one before it.
            #
            # An earlier version also unioned in the N most recent tables,
            # which defeated the limit: after three publishes `reporting` held
            # three versions, because "candidate + previous" and "two most
            # recent" are different sets. Two copies of every mart is already
            # most of a 1 GB free tier (ADR-015), so the retained set has to be
            # the retained set.
            keep = {candidate_version}
            if previous_version:
                keep.add(previous_version)
            assert len(keep) <= VERSIONS_RETAINED, (
                f"retention set {sorted(keep)} exceeds VERSIONS_RETAINED={VERSIONS_RETAINED}"
            )
            for mart in marts:
                _prune_versions(cursor, target_schema, mart, keep)
    except psycopg.Error as error:
        conn.rollback()
        result = finish(
            STATUS_FAILED,
            published_version=previous_version,
            reason=f"the swap failed: {type(error).__name__}: {error}"[:4000],
        )
        raise PublishError(result.reason) from error

    return finish(
        STATUS_PUBLISHED,
        published_version=candidate_version,
        reason=f"{summary.checks_passed}/{summary.checks_run} checks passed",
    )
