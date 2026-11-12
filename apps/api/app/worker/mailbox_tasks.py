"""Pull new mail from each connected mailbox into the inbox."""

import asyncio

from app.services.mailbox import poll_mailbox
from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine
from sqlalchemy import select

from packages.database.models.email_mailbox import EmailMailbox
from packages.database.session import session_scope


async def _poll_mailboxes() -> int:
    ensure_engine()
    async with session_scope() as db:
        ids = list(
            (
                await db.execute(
                    select(EmailMailbox.id).where(EmailMailbox.status == "connected")
                )
            ).scalars().all()
        )
    stored = 0
    for mailbox_id in ids:
        async with session_scope() as db:
            box = await db.get(EmailMailbox, mailbox_id)
            if box is None or box.status != "connected":
                continue
            stored += await poll_mailbox(db, box)
            await db.commit()
    return stored


@celery_app.task(name="mailbox.poll")
def poll_mailboxes_task() -> int:
    return asyncio.run(_poll_mailboxes())
