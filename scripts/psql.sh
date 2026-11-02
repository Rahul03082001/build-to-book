#!/usr/bin/env bash
#
# Run psql against the warehouse.
#
# Uses the host psql client when one is installed, and otherwise the client
# inside the warehouse container -- so a clean machine with nothing but Docker
# can still run `make init`. The project promises that `docker compose up` plus
# the Makefile rebuilds everything; requiring a separate Postgres client
# install would break that promise.
#
# SQL is passed on stdin (`-f -`) rather than by path, because a host path
# means nothing inside the container.
#
# Usage:
#   scripts/psql.sh -c 'select 1'
#   scripts/psql.sh -v name=value -f - < some.sql

set -euo pipefail

: "${POSTGRES_DB:=btb}"
: "${POSTGRES_USER:=btb_admin}"
: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_PASSWORD:=}"

if command -v psql >/dev/null 2>&1; then
    exec env PGPASSWORD="$POSTGRES_PASSWORD" psql \
        --host "$POSTGRES_HOST" \
        --port "$POSTGRES_PORT" \
        --username "$POSTGRES_USER" \
        --dbname "$POSTGRES_DB" \
        --no-password \
        -v ON_ERROR_STOP=1 \
        "$@"
fi

if ! docker compose ps --services --filter status=running 2>/dev/null | grep -qx warehouse; then
    echo "error: psql is not installed and the warehouse container is not running." >&2
    echo "       Run 'make up' first, or install the PostgreSQL client." >&2
    exit 1
fi

exec docker compose exec -T \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    warehouse psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --no-password \
    -v ON_ERROR_STOP=1 \
    "$@"
