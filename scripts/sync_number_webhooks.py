"""Points every client's Twilio numbers at this platform's current address (PUBLIC_BASE_URL).

    python scripts/sync_number_webhooks.py            # report only
    python scripts/sync_number_webhooks.py --apply    # make the changes
    python scripts/sync_number_webhooks.py --tenant acme --apply

Run it after the platform moves to a new server or domain. Only webhooks that are ours are
moved; other providers' addresses on a number are left alone."""

import argparse
import asyncio
import sys

sys.path[:0] = [".", "apps/api"]

from app.core.config import get_settings  # noqa: E402
from app.services.number_webhooks import sync_number_webhooks  # noqa: E402
from sqlalchemy import select  # noqa: E402

from packages.database.models import Tenant  # noqa: E402
from packages.database.session import init_engine, session_scope  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    parser.add_argument("--tenant", help="only this workspace (its slug)")
    args = parser.parse_args()
    init_engine(get_settings().database_url, null_pool=True)
    async with session_scope() as db:
        tenant_id = None
        if args.tenant:
            tenant_id = (await db.execute(select(Tenant.id).where(Tenant.slug == args.tenant))).scalar_one_or_none()
            if tenant_id is None:
                print(f"no workspace with slug {args.tenant!r}")
                return 1
        report = await sync_number_webhooks(db, tenant_id=tenant_id, apply=args.apply)
        await db.commit()
    print(f"target address: {get_settings().public_base_url}  ({'APPLIED' if args.apply else 'dry run, nothing changed'})")
    for row in report:
        print(f"{row['tenant']:<28} {row['number']:<16} {row['status']}" + (f"  {row['error']}" if row["error"] else ""))
        for field, change in row["changes"].items():
            print(f"    {field}: {change['from']}  ->  {change['to']}")
    failed = sum(1 for r in report if r["status"] == "failed")
    print(f"{len(report)} numbers, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
