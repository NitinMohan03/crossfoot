"""The read-only database role the MCP servers connect as (plan §3.3, "Permission boundary").

Every agent-facing tool is read-only and the escalation queue is the only write path. That claim
is worth nothing if it rests on no Python module happening to contain an INSERT, so it is
enforced where it cannot be bypassed: `crossfoot_ro` holds CONNECT, USAGE and SELECT, and no DML
privilege is ever granted to it. A tool that tried to write would fail in the server.

Two consequences worth knowing:

* `erp.sql` begins with `DROP SCHEMA IF EXISTS erp CASCADE`, which takes the grants with it. So
  `just db-load` re-applies these grants afterwards. Forgetting that is how the no-write test
  would quietly become vacuous -- it would be asserting that a role cannot write to a schema it
  cannot see.
* Granting needs a privileged connection, which `just db-roles` has because compose's
  `crossfoot` user owns the database. A real deployment would manage the role out of band.
"""

from collections.abc import Sequence
from typing import Any, Final

import psycopg
from psycopg import sql

from crossfoot.db.connect import RO_PASSWORD, RO_USER

#: The schemas the servers read. Missing ones are skipped, so this runs before `just db-load`.
READ_SCHEMAS: Final = ("erp", "ref", "er")


def _existing_schemas(conn: psycopg.Connection[Any], wanted: Sequence[str]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s) ORDER BY nspname",
            (list(wanted),),
        )
        return [str(row[0]) for row in cur.fetchall()]


def apply_roles(
    conn: psycopg.Connection[Any],
    *,
    user: str = RO_USER,
    password: str = RO_PASSWORD,
    schemas: Sequence[str] = READ_SCHEMAS,
) -> list[str]:
    """Create or refresh the read-only role and its grants. Idempotent; returns what it touched.

    Deliberately grants nothing but CONNECT, USAGE and SELECT. There is no code path here that
    can grant INSERT, UPDATE or DELETE, so a future edit cannot widen the role by accident.
    """
    role = sql.Identifier(user)
    present = _existing_schemas(conn, schemas)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,))
        exists = cur.fetchone() is not None
        verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
        cur.execute(
            sql.SQL("{verb} ROLE {role} LOGIN PASSWORD {password}").format(
                verb=verb, role=role, password=sql.Literal(password)
            )
        )
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {db} TO {role}").format(
                db=sql.Identifier(str(row[0])), role=role
            )
        )
        for name in present:
            schema = sql.Identifier(name)
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {schema} TO {role}").format(schema=schema, role=role)
            )
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}").format(
                    schema=schema, role=role
                )
            )
            # Tables added to this schema later inherit SELECT. Dropped with the schema, which
            # is why a reload of the ERP dump has to come back through here.
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}"
                ).format(schema=schema, role=role)
            )
    conn.commit()
    return present
