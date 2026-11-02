"""The four roles must be denied what they should not reach.

Asserting what a role *can* do is the easy half and the half that usually gets
tested. The half that matters for access control is what it *cannot* do, so
every permitted action here has a matching denial.

Marked `integration`: needs a running warehouse with roles applied.

    make init && .venv/bin/pytest -m integration
"""

from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

ROLES = [
    "btb_loader_svc",
    "btb_transformer_svc",
    "btb_reporter_svc",
    "btb_auditor_svc",
]

PROBE_TABLES = {
    "raw": "raw._role_probe",
    "reporting": "reporting._role_probe",
    "audit": "audit._role_probe",
}

# (role, table, privilege, expected). One row per claim in the role model.
EXPECTED_TABLE_PRIVILEGES = [
    # loader writes raw, and records audit rows without being able to alter them.
    ("btb_loader_svc", "raw._role_probe", "SELECT", True),
    ("btb_loader_svc", "raw._role_probe", "INSERT", True),
    ("btb_loader_svc", "reporting._role_probe", "SELECT", False),
    ("btb_loader_svc", "audit._role_probe", "INSERT", True),
    ("btb_loader_svc", "audit._role_probe", "UPDATE", False),
    ("btb_loader_svc", "audit._role_probe", "DELETE", False),
    # transformer reads raw but does not write it; the loader owns that.
    ("btb_transformer_svc", "raw._role_probe", "SELECT", True),
    ("btb_transformer_svc", "raw._role_probe", "INSERT", False),
    ("btb_transformer_svc", "audit._role_probe", "INSERT", True),
    ("btb_transformer_svc", "audit._role_probe", "UPDATE", False),
    ("btb_transformer_svc", "audit._role_probe", "DELETE", False),
    # reporter reads the certified dataset and nothing else.
    ("btb_reporter_svc", "reporting._role_probe", "SELECT", True),
    ("btb_reporter_svc", "raw._role_probe", "SELECT", False),
    ("btb_reporter_svc", "audit._role_probe", "SELECT", False),
    # auditor tests the controls, so it reads the trail and not the business data.
    ("btb_auditor_svc", "audit._role_probe", "SELECT", True),
    ("btb_auditor_svc", "raw._role_probe", "SELECT", False),
    ("btb_auditor_svc", "reporting._role_probe", "SELECT", False),
]

EXPECTED_SCHEMA_PRIVILEGES = [
    ("btb_reporter_svc", "raw", "USAGE", False),
    ("btb_reporter_svc", "sandbox", "USAGE", False),
    ("btb_reporter_svc", "publish_candidate", "USAGE", False),
    ("btb_reporter_svc", "reporting", "USAGE", True),
    ("btb_auditor_svc", "audit", "USAGE", True),
    ("btb_auditor_svc", "raw", "USAGE", False),
    ("btb_transformer_svc", "sandbox", "USAGE", True),
]


def _psql(*args: str, sql: str | None = None) -> str:
    """Run psql through the repo wrapper, which falls back to the container."""
    result = subprocess.run(
        ["scripts/psql.sh", "-At", *args],
        input=sql,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    if result.returncode != 0:
        pytest.skip(f"warehouse unavailable: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def probe_tables() -> None:
    """Create and drop probe tables.

    Created by the admin role so they inherit the ALTER DEFAULT PRIVILEGES set
    by roles.sql -- which is the mechanism under test. A grant that only works
    on hand-made tables would be a false pass.
    """
    create = "\n".join(
        f"create table if not exists {table} (id int);" for table in PROBE_TABLES.values()
    )
    _psql("-f", "-", sql=create)
    yield
    drop = "\n".join(f"drop table if exists {table};" for table in PROBE_TABLES.values())
    _psql("-f", "-", sql=drop)


@pytest.mark.parametrize(
    ("role", "table", "privilege", "expected"),
    EXPECTED_TABLE_PRIVILEGES,
    ids=lambda v: str(v),
)
def test_table_privilege(role: str, table: str, privilege: str, expected: bool) -> None:
    got = _psql("-c", f"select has_table_privilege('{role}', '{table}', '{privilege}');")
    assert (got == "t") is expected, (
        f"{role} {'should' if expected else 'should NOT'} have {privilege} on {table} (got {got!r})"
    )


@pytest.mark.parametrize(
    ("role", "schema", "privilege", "expected"),
    EXPECTED_SCHEMA_PRIVILEGES,
    ids=lambda v: str(v),
)
def test_schema_privilege(role: str, schema: str, privilege: str, expected: bool) -> None:
    got = _psql("-c", f"select has_schema_privilege('{role}', '{schema}', '{privilege}');")
    assert (got == "t") is expected, (
        f"{role} {'should' if expected else 'should NOT'} have "
        f"{privilege} on schema {schema} (got {got!r})"
    )


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_create_in_public(role: str) -> None:
    """roles.sql revokes the default PUBLIC privileges.

    Without that revoke every role can create objects in `public`, and the
    least-privilege model is decorative.
    """
    got = _psql("-c", f"select has_schema_privilege('{role}', 'public', 'CREATE');")
    assert got == "f", f"{role} can create objects in the public schema"


@pytest.mark.parametrize("role", ROLES)
def test_service_roles_can_log_in_and_group_roles_cannot(role: str) -> None:
    """Privileges live on NOLOGIN group roles; only the _svc users log in.

    That split is what makes a user access review meaningful -- you review
    membership, not scattered per-user grants.
    """
    got = _psql("-c", f"select rolcanlogin from pg_roles where rolname = '{role}';")
    assert got == "t", f"{role} cannot log in"

    group = role.removesuffix("_svc")
    got = _psql("-c", f"select rolcanlogin from pg_roles where rolname = '{group}';")
    assert got == "f", f"group role {group} should be NOLOGIN"


@pytest.mark.parametrize("role", ROLES)
def test_each_service_role_is_a_member_of_exactly_its_group(role: str) -> None:
    got = _psql(
        "-c",
        "select g.rolname from pg_auth_members m "
        "join pg_roles r on r.oid = m.member "
        "join pg_roles g on g.oid = m.roleid "
        f"where r.rolname = '{role}';",
    )
    assert got == role.removesuffix("_svc"), f"{role} has unexpected memberships: {got!r}"


def _psql_as(user: str, password: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Run SQL as a specific role, through the same wrapper the Makefile uses."""
    env = {**os.environ, "POSTGRES_USER": user, "POSTGRES_PASSWORD": password}
    return subprocess.run(
        ["scripts/psql.sh", "-At", "-c", sql],
        capture_output=True,
        text=True,
        env=env,
    )


def test_reporter_is_denied_raw_on_a_real_connection() -> None:
    """has_table_privilege is the model; this is the behaviour.

    Connecting as the role and being refused is the evidence a reviewer wants.
    The permitted read is asserted in the same test on purpose: a denial on its
    own would also "pass" if the connection were simply broken, which would
    prove nothing about privileges.
    """
    password = os.environ.get("BTB_REPORTER_PASSWORD")
    if not password:
        pytest.skip("BTB_REPORTER_PASSWORD not set")

    allowed = _psql_as("btb_reporter_svc", password, "select count(*) from reporting._role_probe;")
    if allowed.returncode != 0 and "permission denied" not in allowed.stderr:
        pytest.skip(f"reporter cannot reach the warehouse: {allowed.stderr.strip()[:200]}")
    assert allowed.returncode == 0, (
        f"reporter should be able to read reporting: {allowed.stderr.strip()[:300]}"
    )

    denied = _psql_as("btb_reporter_svc", password, "select * from raw._role_probe;")
    assert denied.returncode != 0, "reporter was able to read raw"
    # Denial is the property under test, not which layer refuses. Schema USAGE
    # and the table grant are both withheld, so either message is a pass.
    assert "permission denied" in denied.stderr, (
        f"reporter was refused, but not for permissions: {denied.stderr.strip()[:300]}"
    )
