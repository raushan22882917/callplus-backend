"""Connect the mailbox campaigns and the inbox send from."""

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations.oauth.tokens import get_connection
from app.services.mailbox import MailboxError, get_mailbox, save_smtp, use_oauth
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.email_mailbox import EmailMailbox

router = APIRouter(prefix="/email-mailbox", tags=["email-mailbox"])


class MailboxRead(BaseModel):
    mode: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    status: str | None = None
    last_error: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_use_tls: bool = True
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_use_ssl: bool = True
    has_imap: bool = False
    gmail_connected: bool = False
    microsoft_connected: bool = False
    gmail_configured: bool = False
    microsoft_configured: bool = False


class SmtpMailboxIn(BaseModel):
    from_email: str = Field(min_length=3, max_length=255)
    from_name: str | None = Field(default=None, max_length=120)
    smtp_host: str = Field(min_length=1, max_length=255)
    smtp_port: int = 587
    smtp_username: str = Field(min_length=1, max_length=255)
    smtp_password: str | None = Field(default=None, max_length=500)
    smtp_use_tls: bool = True
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = 993
    imap_username: str | None = Field(default=None, max_length=255)
    imap_password: str | None = Field(default=None, max_length=500)
    imap_use_ssl: bool = True


class UseOAuthIn(BaseModel):
    mode: str = Field(pattern="^(gmail|microsoft)$")


def _read(box: EmailMailbox | None, *, gmail: bool, microsoft: bool) -> MailboxRead:
    settings = get_settings()
    return MailboxRead(
        mode=box.mode if box else None,
        from_email=box.from_email if box else None,
        from_name=box.from_name if box else None,
        status=box.status if box else None,
        last_error=box.last_error if box else None,
        smtp_host=box.smtp_host if box else None,
        smtp_port=box.smtp_port if box else None,
        smtp_username=box.smtp_username if box else None,
        smtp_use_tls=box.smtp_use_tls if box else True,
        imap_host=box.imap_host if box else None,
        imap_port=box.imap_port if box else None,
        imap_username=box.imap_username if box else None,
        imap_use_ssl=box.imap_use_ssl if box else True,
        has_imap=bool(box and box.imap_host),
        gmail_connected=gmail,
        microsoft_connected=microsoft,
        gmail_configured=bool(settings.google_client_id and settings.google_client_secret),
        microsoft_configured=bool(settings.microsoft_client_id and settings.microsoft_client_secret),
    )


async def _view(db: AsyncSession, tenant_id) -> MailboxRead:
    box = await get_mailbox(db, tenant_id)
    gmail = await get_connection(db, tenant_id, "gmail")
    microsoft = await get_connection(db, tenant_id, "microsoft")
    return _read(
        box,
        gmail=gmail is not None and bool(gmail.account_email),
        microsoft=microsoft is not None and bool(microsoft.account_email),
    )


@router.get("", response_model=MailboxRead)
async def read_mailbox(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> MailboxRead:
    return await _view(db, ctx.tenant_id)


@router.post("/smtp", response_model=MailboxRead)
async def connect_smtp(
    payload: SmtpMailboxIn,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> MailboxRead:
    try:
        await save_smtp(db, ctx.tenant_id, **payload.model_dump())
    except MailboxError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="email_mailbox.smtp_connected",
        resource_type="email_mailbox",
        resource_id=ctx.tenant_id,
    )
    await db.commit()
    return await _view(db, ctx.tenant_id)


@router.post("/use", response_model=MailboxRead)
async def choose_oauth_mailbox(
    payload: UseOAuthIn,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> MailboxRead:
    try:
        await use_oauth(db, ctx.tenant_id, payload.mode)
    except MailboxError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="email_mailbox.oauth_selected",
        resource_type="email_mailbox",
        resource_id=ctx.tenant_id,
        extra={"mode": payload.mode},
    )
    await db.commit()
    return await _view(db, ctx.tenant_id)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mailbox(
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    box = await get_mailbox(db, ctx.tenant_id)
    if box is not None:
        await db.delete(box)
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action="email_mailbox.disconnected",
            resource_type="email_mailbox",
            resource_id=box.id,
        )
        await db.commit()
