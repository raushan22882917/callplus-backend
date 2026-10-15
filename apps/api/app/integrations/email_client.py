"""Transactional email (password reset, email verification).

No SMTP provider has been picked for this project yet, so when smtp_host is
unset (the dev default) send_email logs the message instead of raising —
callers (auth.py) treat email delivery as best-effort, so this keeps local
dev/tests working without a real mail server rather than blocking signup or
forgot-password on infrastructure that isn't configured.
"""

import logging
from email.message import EmailMessage

import aiosmtplib
from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def send_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    headers: dict[str, str] | None = None,
) -> None:
    """`headers` carries extras such as Message-ID / In-Reply-To / References /
    Reply-To (used by the email channel to keep replies in one thread)."""
    settings = get_settings()

    if not settings.smtp_host:
        logger.info(
            "email (SMTP not configured) to=%s subject=%r body=%s", to_email, subject, html_body
        )
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(html_body, subtype="html")

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username or None,
        password=settings.smtp_password or None,
        start_tls=settings.smtp_use_tls,
    )
