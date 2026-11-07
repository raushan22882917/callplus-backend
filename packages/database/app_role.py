"""Create the database role the API and workers connect as.

Postgres superusers ignore row-level security, and the official image makes
POSTGRES_USER a superuser. Migrations keep using that owner. The serving
process uses `callcenter_app`, which has no superuser bit and no BYPASSRLS,
so the tenant policies actually apply.

The container entrypoint runs this with MIGRATION_DATABASE_URL (the owner)
after `alembic upgrade`. APP_DB_PASSWORD is the role's password, and it is
the password in the serving DATABASE_URL.
"""

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

APP_ROLE = "callcenter_app"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _database_name(url: str) -> str:
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name.replace("_", "").isalnum():
        raise RuntimeError(f"refusing to grant on unexpected database name {name!r}")
    return name


async def _ensure(url: str, password: str) -> None:
    database = _database_name(url)
    password_sql = _quote(password)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": APP_ROLE}
                )
            ).first()
            attributes = (
                f"LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {password_sql}"
            )
            if exists is None:
                await conn.execute(text(f"CREATE ROLE {APP_ROLE} {attributes}"))
            else:
                await conn.execute(text(f"ALTER ROLE {APP_ROLE} WITH {attributes}"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {database} TO {APP_ROLE}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f"TO {APP_ROLE}"
                )
            )
            await conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
                )
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
                )
            )
    finally:
        await engine.dispose()


def ensure_app_role() -> None:
    url = os.environ.get("DATABASE_URL", "")
    password = os.environ.get("APP_DB_PASSWORD", "")
    if not url:
        raise SystemExit("DATABASE_URL is required")
    if not password:
        raise SystemExit("APP_DB_PASSWORD is required to create the application database role")
    import asyncio

    asyncio.run(_ensure(url, password))
