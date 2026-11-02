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
    }
    return " ".join(f"{key}={value}" for key, value in parts.items() if value != "")


def connect(role: str | None = None) -> psycopg.Connection:
    """Open a connection with transactions under our control.

    ``autocommit=False`` is deliberate: the loader manages its own
    transactions so the staging table, the insert and the reject log commit
    together, while the audit row commits separately and survives a failure.
    """
    return psycopg.connect(dsn(role), autocommit=False)
