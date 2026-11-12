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
import httpx
from app.core.config import get_settings
from app.integrations.email_providers import EmailApiCredentials

logger = logging.getLogger(__name__)


async def send_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    headers: dict[str, str] | None = None,
    tenant_id: str | None = None,
    credentials: EmailApiCredentials | None = None,
) -> None:
    """`headers` carries extras such as Message-ID / In-Reply-To / References /
    Reply-To (used by the email channel to keep replies in one thread).
    `tenant_id` is sent to the HTTPS providers as a tag so a bounce or complaint
    event can be matched back to the workspace."""
    settings = get_settings()

    if credentials is not None:
        # The workspace's own Resend or Brevo account, from its own address.
        await _send_via_api(
            credentials.provider, credentials.api_key, credentials.from_email, to_email, subject, html_body,
            headers or {}, tenant_id, credentials.from_name,
        )
        return

    if settings.email_api_key:
        await _send_via_api(
            settings.email_api_provider,
            settings.email_api_key,
            settings.smtp_from_email,
            to_email,
            subject,
            html_body,
            headers or {},
            tenant_id,
        )
        return

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

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_use_tls,
            timeout=15,
        )
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(
            "The mail server did not answer. This host blocks outbound SMTP. "
            "Connect Gmail or Microsoft, or set EMAIL_API_KEY."
        ) from exc


async def _send_via_api(
    provider: str,
    api_key: str,
    from_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    headers: dict[str, str],
    tenant_id: str | None = None,
    from_name: str | None = None,
) -> None:
    """Send over HTTPS. Raises on a rejected request so callers can report it."""
    provider = (provider or "resend").lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        if provider == "brevo":
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": api_key, "accept": "application/json"},
                json={
                    "sender": {"email": from_email, **({"name": from_name} if from_name else {})},
                    "to": [{"email": to_email}],
                    "subject": subject,
                    "htmlContent": html_body,
                    **({"headers": headers} if headers else {}),
                    **({"tags": [f"tenant_{tenant_id}"]} if tenant_id else {}),
                },
            )
        else:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": f"{from_name} <{from_email}>" if from_name else from_email,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_body,
                    **({"headers": headers} if headers else {}),
                    **({"tags": [{"name": "tenant", "value": tenant_id}]} if tenant_id else {}),
                },
            )
    if resp.status_code >= 400:
        raise RuntimeError(f"email provider {provider} rejected the message ({resp.status_code})")
