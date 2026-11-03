"""Connection handling for the warehouse.

Keeps DSN assembly in one place so no credential is ever interpolated into a
log line or an error message. Prefers a single hosted DSN when one is set,
because assembling a hosted connection from separate variables is how a
password ends up in a traceback.
"""

from __future__ import annotations

import os

import psycopg

# Loading writes to raw and appends to the audit trail, so the loader connects
# as the loader role when its password is available. Falling back to the admin
# user keeps local development working before `make roles` has been run.
LOADER_ROLE = "btb_loader_svc"


def dsn(role: str | None = None) -> str:
    """Build a connection string from the environment.

    Args:
        role: Connect as this role instead of the default user. The matching
            password is read from ``BTB_<ROLE>_PASSWORD``.

    Returns:
        A libpq connection string.
    """
    hosted = os.environ.get("BTB_HOSTED_DSN")
    if hosted:
        return hosted

    user = os.environ.get("POSTGRES_USER", "btb_admin")
    password = os.environ.get("POSTGRES_PASSWORD", "")

    if role == LOADER_ROLE:
        role_password = os.environ.get("BTB_LOADER_PASSWORD")
        if role_password:
            user, password = LOADER_ROLE, role_password

    parts = {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "dbname": os.environ.get("POSTGRES_DB", "btb"),
        "user": user,
        "password": password,
        # Fail fast when nothing answers.
        #
        # Without this libpq waits indefinitely, and a database that ACCEPTS a
        # connection and then never replies hangs rather than erroring -- which
        # is not hypothetical. Docker Desktop was installed on an external
        # drive; the drive disconnected, the daemon wedged, and its port
        # forwarder went on accepting TCP while Postgres was gone. The test
        # suite's skip path catches OperationalError, but a hang never raises
        # one, so a 100-second run sat blocked for fifteen minutes and reported
        # nothing at all.
        #
        # A bounded wait turns that into a skip with a reason. Overridable,
        # because a cold hosted instance can legitimately take longer to accept
        # than a local container.
        "connect_timeout": os.environ.get("BTB_CONNECT_TIMEOUT", "10"),
    }
    return " ".join(f"{key}={value}" for key, value in parts.items() if value != "")


def connect(role: str | None = None) -> psycopg.Connection:
    """Open a connection with transactions under our control.

    ``autocommit=False`` is deliberate: the loader manages its own
    transactions so the staging table, the insert and the reject log commit
    together, while the audit row commits separately and survives a failure.

    Raises:
        psycopg.OperationalError: if the database does not answer within
            ``connect_timeout`` (see `dsn`). Callers that treat an unreachable
            warehouse as a skip depend on this being an error rather than a
            wait.
    """
    return psycopg.connect(dsn(role), autocommit=False)
