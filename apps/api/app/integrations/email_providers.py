"""A workspace's own email sending account: Resend or Brevo.

The owner pastes an API key and the address mail should come from. We ask the provider whether it accepts the key and
whether that address is one it will send from, before anything is saved. The key is then stored encrypted and used for
this workspace's customer email (campaigns, follow-ups, inbox replies). Password reset and other platform mail still
use the platform's own sender."""

from dataclasses import dataclass

import httpx

PROVIDERS = ("resend", "brevo")
LABEL = {"resend": "Resend", "brevo": "Brevo"}


class EmailProviderError(Exception):
    pass


@dataclass(frozen=True)
class EmailApiCredentials:
    provider: str
    api_key: str
    from_email: str
    from_name: str | None = None


def _domain(address: str) -> str:
    return address.rpartition("@")[2].strip().lower()


async def check_sender(provider: str, api_key: str, from_email: str) -> str | None:
    """Raises EmailProviderError with a plain-words reason when the key or address will not work. Returns a note
    (never an error) when the key is valid but cannot be checked further."""
    key = (api_key or "").strip()
    if not key:
        raise EmailProviderError("Paste your API key.")
    if provider not in PROVIDERS:
        raise EmailProviderError("Choose Resend or Brevo.")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if provider == "resend":
                return _check_resend(await client.get("https://api.resend.com/domains", headers={"Authorization": f"Bearer {key}"}), from_email)
            return _check_brevo(await client.get("https://api.brevo.com/v3/senders", headers={"api-key": key, "accept": "application/json"}), from_email)
    except httpx.HTTPError as exc:
        raise EmailProviderError(f"We could not reach {LABEL[provider]}. Please try again in a moment.") from exc


def _check_resend(response: httpx.Response, from_email: str) -> str | None:
    if response.status_code == 401:
        try:
            name = (response.json() or {}).get("name")
        except ValueError:
            name = None
        if name == "restricted_api_key":
            # A key limited to sending cannot list domains. It can still send, so accept it.
            return "This key can send but cannot list your domains, so the sender address could not be checked."
        raise EmailProviderError("Resend did not accept this key. Copy it again from the Resend dashboard.")
    if response.status_code >= 400:
        raise EmailProviderError(f"Resend answered with an error ({response.status_code}). Please try again.")
    domains = {str(d.get("name", "")).lower(): str(d.get("status", "")).lower() for d in (response.json().get("data") or [])}
    want = _domain(from_email)
    if want not in domains:
        raise EmailProviderError(f"{want} is not a domain on this Resend account. Add and verify it in Resend first.")
    if domains[want] != "verified":
        raise EmailProviderError(f"{want} is not verified in Resend yet. Finish the DNS steps there, then try again.")
    return None


def _check_brevo(response: httpx.Response, from_email: str) -> str | None:
    if response.status_code in (401, 403):
        raise EmailProviderError("Brevo did not accept this key. Copy it again from Brevo, SMTP and API.")
    if response.status_code >= 400:
        raise EmailProviderError(f"Brevo answered with an error ({response.status_code}). Please try again.")
    senders = {str(s.get("email", "")).lower(): bool(s.get("active")) for s in (response.json().get("senders") or [])}
    want = from_email.strip().lower()
    if want not in senders:
        raise EmailProviderError(f"{from_email} is not a sender on this Brevo account. Add and verify it in Brevo first.")
    if not senders[want]:
        raise EmailProviderError(f"{from_email} is not active in Brevo yet. Confirm it there, then try again.")
    return None
