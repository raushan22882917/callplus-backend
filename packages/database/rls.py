"""Postgres row-level security as a backstop to the application's own
tenant_id filtering (see app/core/repository.py).

How it works: an authenticated, tenant-scoped API request sets the
transaction-local setting `app.tenant_id` (packages/database/session.py,
set_tenant_context). Every tenant table's policies then only let that
tenant's rows through, so a query that forgets its tenant_id filter returns
nothing from other tenants instead of leaking them.

When `app.tenant_id` is unset the policies allow everything. That is
deliberate: webhooks (tenant derived from a phone number), Celery workers,
signup and platform-admin requests legitimately run without a tenant
context, and the app-level filtering still protects them.

IMPORTANT: Postgres superusers (and roles with BYPASSRLS) ignore RLS
entirely. Production must connect as a normal role; FORCE ROW LEVEL SECURITY
below makes policies apply to the table owner too.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

_CTX = "nullif(current_setting('app.tenant_id', true), '')"
POLICY_PREFIX = "tenant_isolation"


def _tenant_tables(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public'
              AND c.column_name = 'tenant_id'
              AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name
            """
        )
    )
    return [r[0] for r in rows]


def _enable(conn: Connection, table: str, key: str, *, nullable_rows_readable: bool) -> None:
    conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    conn.execute(text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    match = f"{key} = {_CTX}::uuid"
    unset = f"{_CTX} IS NULL"
    read = f"{unset} OR {match}" + (f" OR {key} IS NULL" if nullable_rows_readable else "")
    write = f"{unset} OR {match}"
    # Reads may also see tenant-less system rows (e.g. system roles); writes
    # may not touch them from a tenant context.
    conn.execute(
        text(f'CREATE POLICY {POLICY_PREFIX}_select ON "{table}" FOR SELECT USING ({read})')
    )
    conn.execute(
        text(f'CREATE POLICY {POLICY_PREFIX}_insert ON "{table}" FOR INSERT WITH CHECK ({write})')
    )
    conn.execute(
        text(
            f'CREATE POLICY {POLICY_PREFIX}_update ON "{table}" FOR UPDATE '
            f"USING ({write}) WITH CHECK ({write})"
        )
    )
    conn.execute(
        text(f'CREATE POLICY {POLICY_PREFIX}_delete ON "{table}" FOR DELETE USING ({write})')
    )


def _disable(conn: Connection, table: str) -> None:
    for command in ("select", "insert", "update", "delete"):
        conn.execute(text(f'DROP POLICY IF EXISTS {POLICY_PREFIX}_{command} ON "{table}"'))
    conn.execute(text(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'))
    conn.execute(text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'))


def apply_rls(conn: Connection) -> list[str]:
    """Enable RLS on every table with a tenant_id column, plus `tenants`
    itself (keyed by id). Returns the tables protected."""
    tables = _tenant_tables(conn)
    for table in tables:
        _enable(conn, table, "tenant_id", nullable_rows_readable=True)
    _enable(conn, "tenants", "id", nullable_rows_readable=False)
    return [*tables, "tenants"]


def remove_rls(conn: Connection) -> None:
    for table in [*_tenant_tables(conn), "tenants"]:
        _disable(conn, table)
