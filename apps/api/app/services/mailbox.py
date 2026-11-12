"""A workspace's own mailbox: Gmail, Microsoft 365, or IMAP/SMTP they type in.

Outbound mail (campaigns, inbox replies, follow-ups) is sent from this account.
Inbound mail is polled into the inbox, and the email agent answers when one is on.
Passwords are encrypted. OAuth tokens stay on the oauth connection.
"""

import asyncio
import base64
import email
import imaplib
import ipaddress
import logging
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr

import aiosmtplib
import httpx
from app.core.crypto import decrypt_secret, encrypt_secret
from app.integrations.channel_payloads import InboundEmail, strip_quoted_reply
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.tokens import get_connection, get_valid_access_token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.email_mailbox import EmailMailbox
from packages.database.models.oauth import OAuthConnection

logger = logging.getLogger(__name__)

GMAIL = "gmail"
MICROSOFT = "microsoft"
SMTP = "smtp"
_POLL_LIMIT = 15


class MailboxError(Exception):
    """A problem the person who connected the mailbox can act on."""


def assert_mail_host(host: str) -> str:
    """A public mail hostname. Blocks loopback, link-local and private addresses."""
    cleaned = (host or "").strip().lower().rstrip(".")
    if not cleaned or len(cleaned) > 255 or any(c in cleaned for c in " /\\"):
        raise MailboxError("Enter the mail server's hostname, such as smtp.gmail.com.")
    if cleaned in {"localhost", "metadata.google.internal"} or cleaned.endswith(".local"):
        raise MailboxError("That mail server is not allowed.")
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return cleaned
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise MailboxError("That mail server is not allowed.")
    return cleaned


def _port(value: int, label: str) -> int:
    if not isinstance(value, int) or value < 1 or value > 65535:
        raise MailboxError(f"{label} port must be between 1 and 65535.")
    return value


async def get_mailbox(db: AsyncSession, tenant_id: uuid.UUID) -> EmailMailbox | None:
    return (
        await db.execute(select(EmailMailbox).where(EmailMailbox.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def connected_mailbox(db: AsyncSession, tenant_id: uuid.UUID) -> EmailMailbox | None:
    box = await get_mailbox(db, tenant_id)
    if box is None or box.status != "connected":
        return None
    return box


def _from_header(box: EmailMailbox) -> str:
    if box.from_name:
        return f"{box.from_name} <{box.from_email}>"
    return box.from_email


def build_message(
    *,
    sender: str,
    to_email: str,
    subject: str,
    text: str,
    html: str,
    message_id: str,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if reply_to:
        message["Reply-To"] = reply_to
    for name, value in (extra_headers or {}).items():
        message[name] = value
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join([*(references or []), in_reply_to][-10:])
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _message_id(from_email: str) -> str:
    domain = from_email.rpartition("@")[2] or "localhost"
    return f"<{uuid.uuid4().hex}@{domain}>"


async def deliver(
    db: AsyncSession,
    box: EmailMailbox,
    *,
    to_email: str,
    subject: str,
    text: str,
    html: str,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Sends one message from the mailbox. Returns the Message-ID. Extra headers
    (List-Unsubscribe) go on Gmail and SMTP mail; Microsoft Graph accepts none."""
    if box.status != "connected":
        raise MailboxError(box.last_error or "The mailbox needs to be connected again.")
    message_id = message_id or _message_id(box.from_email)
    mime = build_message(
        sender=_from_header(box),
        to_email=to_email,
        subject=subject,
        text=text,
        html=html,
        message_id=message_id,
        reply_to=reply_to,
        in_reply_to=in_reply_to,
        references=references,
        extra_headers=extra_headers,
    )
    try:
        if box.mode == GMAIL:
            await _gmail_send(db, box, mime)
        elif box.mode == MICROSOFT:
            await _microsoft_send(db, box, to_email, subject, html, reply_to)
        elif box.mode == SMTP:
            await _smtp_send(box, mime)
        else:
            raise MailboxError("This mailbox is not set up.")
    except MailboxError:
        raise
    except Exception as exc:
        logger.warning("mailbox send failed for tenant %s: %s", box.tenant_id, type(exc).__name__)
        raise MailboxError(f"The mailbox refused the message: {_public(exc)}") from exc
    return message_id


async def attach_oauth(db: AsyncSession, conn: OAuthConnection) -> EmailMailbox | None:
    """Makes a freshly connected Gmail or Microsoft account the sending mailbox."""
    if conn.provider not in (GMAIL, MICROSOFT):
        return None
    if not conn.account_email:
        return None
    box = await get_mailbox(db, conn.tenant_id)
    if box is None:
        box = EmailMailbox(tenant_id=conn.tenant_id, mode=conn.provider, from_email=conn.account_email)
        db.add(box)
    box.mode = conn.provider
    box.from_email = conn.account_email
    box.from_name = conn.account_label if conn.account_label != conn.account_email else box.from_name
    box.status = "connected"
    box.last_error = None
    _clear_passwords(box)
    await db.flush()
    return box


async def detach_oauth(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> None:
    box = await get_mailbox(db, tenant_id)
    if box is not None and box.mode == provider:
        await db.delete(box)
        await db.flush()


def _clear_passwords(box: EmailMailbox) -> None:
    box.smtp_host = box.smtp_username = box.encrypted_smtp_password = None
    box.smtp_port = None
    box.imap_host = box.imap_username = box.encrypted_imap_password = None
    box.imap_port = box.imap_last_uid = None
    box.imap_uidvalidity = None


async def save_smtp(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    from_email: str,
    from_name: str | None,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str | None,
    smtp_use_tls: bool,
    imap_host: str | None,
    imap_port: int | None,
    imap_username: str | None,
    imap_password: str | None,
    imap_use_ssl: bool,
) -> EmailMailbox:
    address = from_email.strip().lower()
    if "@" not in address or len(address) > 255:
        raise MailboxError("Enter the email address this mailbox sends from.")
    host = assert_mail_host(smtp_host)
    port = _port(smtp_port, "SMTP")
    username = smtp_username.strip()
    if not username:
        raise MailboxError("Enter the SMTP username.")
    box = await get_mailbox(db, tenant_id)
    password = smtp_password or ""
    if not password:
        if box is None or not box.encrypted_smtp_password:
            raise MailboxError("Enter the SMTP password.")
        password = decrypt_secret(box.encrypted_smtp_password)
    await _test_smtp(host, port, username, password, smtp_use_tls)

    imap_password_stored = None
    cleaned_imap = None
    if (imap_host or "").strip():
        cleaned_imap = assert_mail_host(imap_host or "")
        imap_port = _port(imap_port or 993, "IMAP")
        imap_user = (imap_username or username).strip()
        imap_secret = imap_password or ""
        if not imap_secret and box is not None and box.encrypted_imap_password:
            imap_secret = decrypt_secret(box.encrypted_imap_password)
        if not imap_secret:
            raise MailboxError("Enter the IMAP password, or leave IMAP blank to only send.")
        await asyncio.to_thread(_test_imap, cleaned_imap, imap_port, imap_user, imap_secret, imap_use_ssl)
        imap_password_stored = encrypt_secret(imap_secret)
        imap_username = imap_user
    else:
        imap_port = None
        imap_username = None

    if box is None:
        box = EmailMailbox(tenant_id=tenant_id, mode=SMTP, from_email=address)
        db.add(box)
    box.mode = SMTP
    box.from_email = address
    box.from_name = (from_name or "").strip() or None
    box.status = "connected"
    box.last_error = None
    box.smtp_host = host
    box.smtp_port = port
    box.smtp_username = username
    box.encrypted_smtp_password = encrypt_secret(password)
    box.smtp_use_tls = smtp_use_tls
    box.imap_host = cleaned_imap
    box.imap_port = imap_port
    box.imap_username = imap_username
    box.encrypted_imap_password = imap_password_stored
    box.imap_use_ssl = imap_use_ssl
    if not cleaned_imap:
        box.imap_uidvalidity = None
        box.imap_last_uid = None
    await db.flush()
    return box


async def use_oauth(db: AsyncSession, tenant_id: uuid.UUID, mode: str) -> EmailMailbox:
    if mode not in (GMAIL, MICROSOFT):
        raise MailboxError("Choose Gmail or Microsoft.")
    conn = await get_connection(db, tenant_id, mode)
    if conn is None or not conn.account_email:
        label = "Gmail" if mode == GMAIL else "Microsoft"
        raise MailboxError(f"Connect {label} first.")
    box = await attach_oauth(db, conn)
    if box is None:
        raise MailboxError("That account has no email address.")
    return box


def _smtp_flags(port: int, use_tls: bool) -> tuple[bool, bool]:
    implicit = port == 465
    return implicit, use_tls and not implicit


async def _test_smtp(host: str, port: int, username: str, password: str, use_tls: bool) -> None:
    implicit, start = _smtp_flags(port, use_tls)
    client = aiosmtplib.SMTP(hostname=host, port=port, use_tls=implicit, start_tls=start, timeout=20)
    try:
        await client.connect()
        await client.login(username, password)
    except Exception as exc:
        if type(exc).__name__ in {"SMTPConnectTimeoutError", "SMTPConnectError", "TimeoutError"}:
            raise MailboxError(
                "The mail server did not answer. This host blocks outbound SMTP. Sign in with Gmail or Microsoft instead."
            ) from exc
        raise MailboxError(f"Could not sign in to SMTP: {_public(exc)}") from exc
    finally:
        try:
            await client.quit()
        except Exception:
            pass


def _test_imap(host: str, port: int, username: str, password: str, use_ssl: bool) -> None:
    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    try:
        client.login(username, password)
        client.select("INBOX", readonly=True)
    except Exception as exc:
        raise MailboxError(f"Could not sign in to IMAP: {_public(exc)}") from exc
    finally:
        try:
            client.logout()
        except Exception:
            pass


async def _smtp_send(box: EmailMailbox, mime: EmailMessage) -> None:
    if not box.smtp_host or not box.encrypted_smtp_password or not box.smtp_username:
        raise MailboxError("SMTP is not filled in.")
    port = box.smtp_port or 587
    implicit, start = _smtp_flags(port, box.smtp_use_tls)
    await aiosmtplib.send(
        mime,
        hostname=box.smtp_host,
        port=port,
        username=box.smtp_username,
        password=decrypt_secret(box.encrypted_smtp_password),
        use_tls=implicit,
        start_tls=start,
        timeout=30,
    )


async def _gmail_send(db: AsyncSession, box: EmailMailbox, mime: EmailMessage) -> None:
    token = await _token(db, box.tenant_id, GMAIL)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
        )
    if response.status_code >= 400:
        raise MailboxError(f"Gmail refused the message ({response.status_code}).")


async def _microsoft_send(
    db: AsyncSession, box: EmailMailbox, to_email: str, subject: str, html: str, reply_to: str | None
) -> None:
    token = await _token(db, box.tenant_id, MICROSOFT)
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if reply_to:
        message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message, "saveToSentItems": True},
        )
    if response.status_code >= 400:
        raise MailboxError(f"Microsoft refused the message ({response.status_code}).")


async def _token(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> str:
    try:
        return await get_valid_access_token(db, tenant_id, provider)
    except OAuthError as exc:
        raise MailboxError(exc.message) from exc


def _public(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return (text[:240] or exc.__class__.__name__)


# ---------------------------------------------------------------- inbound


async def poll_mailbox(db: AsyncSession, box: EmailMailbox) -> int:
    """Pulls new mail into the inbox. Returns how many new messages were stored."""
    if box.status != "connected":
        return 0
    try:
        if box.mode == GMAIL:
            count = await _poll_gmail(db, box)
        elif box.mode == MICROSOFT:
            count = await _poll_microsoft(db, box)
        elif box.mode == SMTP and box.imap_host and box.encrypted_imap_password:
            count = await _poll_imap(db, box)
        else:
            return 0
    except Exception as exc:
        box.last_error = _public(exc)[:500]
        if isinstance(exc, MailboxError) and "sign in" in box.last_error.lower():
            box.status = "error"
        await db.flush()
        logger.warning("mailbox poll failed for %s: %s", box.from_email, box.last_error)
        return 0
    box.last_error = None
    box.last_inbound_at = datetime.now(UTC)
    await db.flush()
    return count


async def _poll_gmail(db: AsyncSession, box: EmailMailbox) -> int:
    token = await _token(db, box.tenant_id, GMAIL)
    async with httpx.AsyncClient(timeout=30) as client:
        listed = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": "is:unread in:inbox", "maxResults": _POLL_LIMIT},
        )
        if listed.status_code >= 400:
            raise MailboxError(f"Gmail inbox could not be read ({listed.status_code}).")
        ids = [row["id"] for row in listed.json().get("messages") or [] if row.get("id")]
        stored = 0
        for message_id in ids:
            got = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "raw"},
            )
            if got.status_code >= 400:
                continue
            raw = got.json().get("raw")
            if not raw:
                continue
            parsed = _parse_raw(base64.urlsafe_b64decode(raw.encode()))
            if parsed is None:
                continue
            outcome = await ingest(db, box, parsed)
            if outcome != "ignored":
                await client.post(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/modify",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"removeLabelIds": ["UNREAD"]},
                )
            if outcome == "ok":
                stored += 1
        return stored


async def _poll_microsoft(db: AsyncSession, box: EmailMailbox) -> int:
    token = await _token(db, box.tenant_id, MICROSOFT)
    async with httpx.AsyncClient(timeout=30) as client:
        listed = await client.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "$filter": "isRead eq false",
                "$top": str(_POLL_LIMIT),
                "$select": "id,subject,from,body,internetMessageId,toRecipients",
            },
        )
        if listed.status_code >= 400:
            raise MailboxError(f"Microsoft inbox could not be read ({listed.status_code}).")
        stored = 0
        for item in listed.json().get("value") or []:
            sender = ((item.get("from") or {}).get("emailAddress") or {})
            body = item.get("body") or {}
            text = body.get("content") or ""
            if (body.get("contentType") or "").lower() == "html":
                text = _html_to_text(text)
            to_list = item.get("toRecipients") or [{}]
            to_address = ((to_list[0].get("emailAddress") or {}).get("address") if to_list else "") or box.from_email
            parsed = InboundEmail(
                from_address=(sender.get("address") or "").lower(),
                from_name=sender.get("name") or "",
                to_address=to_address,
                subject=item.get("subject") or "",
                text=strip_quoted_reply(text),
                message_id=item.get("internetMessageId"),
                in_reply_to=None,
                references=[],
            )
            if not parsed.from_address or not parsed.text:
                continue
            outcome = await ingest(db, box, parsed)
            if outcome != "ignored":
                await client.patch(
                    f"https://graph.microsoft.com/v1.0/me/messages/{item['id']}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"isRead": True},
                )
            if outcome == "ok":
                stored += 1
        return stored


async def _poll_imap(db: AsyncSession, box: EmailMailbox) -> int:
    password = decrypt_secret(box.encrypted_imap_password or "")
    fetched = await asyncio.to_thread(
        _imap_fetch,
        box.imap_host or "",
        box.imap_port or 993,
        box.imap_username or box.smtp_username or "",
        password,
        box.imap_use_ssl,
    )
    stored = 0
    seen: list[str] = []
    for uid, raw in fetched:
        parsed = _parse_raw(raw)
        if parsed is None:
            seen.append(uid)
            continue
        outcome = await ingest(db, box, parsed)
        if outcome != "ignored":
            seen.append(uid)
        if outcome == "ok":
            stored += 1
    if seen:
        await asyncio.to_thread(
            _imap_mark_seen,
            box.imap_host or "",
            box.imap_port or 993,
            box.imap_username or box.smtp_username or "",
            password,
            box.imap_use_ssl,
            seen,
        )
    return stored


def _imap_client(host: str, port: int, username: str, password: str, use_ssl: bool) -> imaplib.IMAP4:
    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    client.login(username, password)
    return client


def _imap_fetch(host: str, port: int, username: str, password: str, use_ssl: bool) -> list[tuple[str, bytes]]:
    client = _imap_client(host, port, username, password, use_ssl)
    try:
        client.select("INBOX", readonly=True)
        status, data = client.uid("search", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-_POLL_LIMIT:]
        messages: list[tuple[str, bytes]] = []
        for uid in uids:
            status, fetched = client.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not fetched:
                continue
            raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
            if isinstance(raw, bytes):
                messages.append((uid.decode(), raw))
        return messages
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _imap_mark_seen(host: str, port: int, username: str, password: str, use_ssl: bool, uids: list[str]) -> None:
    client = _imap_client(host, port, username, password, use_ssl)
    try:
        client.select("INBOX")
        for uid in uids:
            client.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _parse_raw(raw: bytes) -> InboundEmail | None:
    parsed = email.message_from_bytes(raw)
    name, address = parseaddr(parsed.get("From", ""))
    address = address.strip().lower()
    if not address:
        return None
    text = ""
    html = ""
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_disposition() == "attachment":
                continue
            kind = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if kind == "text/plain" and not text:
                text = decoded
            elif kind == "text/html" and not html:
                html = decoded
    else:
        payload = parsed.get_payload(decode=True) or b""
        decoded = payload.decode(parsed.get_content_charset() or "utf-8", errors="replace")
        if parsed.get_content_type() == "text/html":
            html = decoded
        else:
            text = decoded
    body = strip_quoted_reply(text or _html_to_text(html))
    if not body:
        return None
    references = parsed.get("References", "")
    return InboundEmail(
        from_address=address,
        from_name=name,
        to_address=parseaddr(parsed.get("To", ""))[1],
        subject=parsed.get("Subject", "") or "",
        text=body,
        message_id=(parsed.get("Message-ID") or None),
        in_reply_to=(parsed.get("In-Reply-To") or None),
        references=[token for token in references.split() if token],
        is_automated=_automated(parsed),
    )


def _automated(parsed: email.message.Message) -> bool:
    auto = (parsed.get("Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return True
    precedence = (parsed.get("Precedence") or "").lower()
    return precedence in {"bulk", "list", "junk"}


def _html_to_text(html: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


async def ingest(db: AsyncSession, box: EmailMailbox, mail: InboundEmail) -> str:
    """Stores one inbound message. Returns ok, duplicate, or ignored."""
    from app.services import channels as channel_svc
    from packages.database.models import Conversation, ConversationChannel

    if mail.is_automated or not mail.from_address or mail.from_address == box.from_email.lower():
        return "ignored"
    if mail.message_id and await channel_svc.message_already_seen(db, box.tenant_id, mail.message_id):
        return "duplicate"
    customer, created = await channel_svc.find_or_create_customer_by_email(
        db, box.tenant_id, mail.from_address, mail.from_name or None
    )
    conversation = None
    conversation_id = await channel_svc.find_email_conversation_id(
        db, box.tenant_id, [*([mail.in_reply_to] if mail.in_reply_to else []), *mail.references]
    )
    if conversation_id is not None:
        conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        conversation, _, _ = await channel_svc.ensure_thread(
            db,
            box.tenant_id,
            ConversationChannel.EMAIL,
            (mail.message_id or f"mail:{uuid.uuid4().hex}")[:255],
            customer_id=customer.id,
            contact_address=mail.from_address,
            via_address=box.from_email,
            subject=mail.subject or "(no subject)",
        )
    settings = await channel_svc.get_channel_settings(db, box.tenant_id, "email")
    agent = None
    if settings is not None and settings.enabled:
        agent = await channel_svc.resolve_agent(db, box.tenant_id, settings.ai_agent_id)
    outcome = await channel_svc.process_inbound_text(
        db,
        tenant_id=box.tenant_id,
        agent=agent,
        conversation=conversation,
        text=mail.text,
        provider_message_id=mail.message_id,
        customer_created=created,
        source="email",
    )
    if agent is not None and outcome.reply_text:
        await channel_svc.send_ai_reply(db, outcome)
    return "ok"
