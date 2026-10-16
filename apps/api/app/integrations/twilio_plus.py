"""Twilio REST helpers for telephony_plus: Elastic SIP trunk sync, queue/park
call control. Thin wrappers around twilio_client._client so credentials
resolution (platform vs BYO) is identical to the rest of the platform; a
missing account raises TwilioClientError with a clear message."""

from dataclasses import dataclass

from app.integrations.credentials import TwilioCredentials
from app.integrations.twilio_client import TwilioClientError, _client, redirect_live


@dataclass(frozen=True)
class TrunkSyncResult:
    trunk_sid: str
    domain_name: str
    origination_sid: str | None
    number_attached: bool


def validate_trunk_domain(termination_uri: str) -> str:
    domain = termination_uri.strip().lower().removeprefix("sip:")
    if not domain.endswith(".pstn.twilio.com"):
        raise TwilioClientError(
            "Twilio Elastic SIP termination URI must look like <name>.pstn.twilio.com"
        )
    return domain


def sync_elastic_sip_trunk(
    *,
    label: str,
    termination_uri: str,
    origination_uri: str | None,
    caller_id: str | None,
    existing_trunk_sid: str | None,
    credentials: TwilioCredentials | None = None,
) -> TrunkSyncResult:
    """Creates (or updates) the Elastic SIP trunk, its origination URL and
    attaches the caller-id number when it is one of the account's numbers."""
    domain = validate_trunk_domain(termination_uri)
    client = _client(credentials)
    trunks = client.trunking.v1.trunks

    if existing_trunk_sid:
        trunk = trunks(existing_trunk_sid).update(friendly_name=label, domain_name=domain)
    else:
        trunk = trunks.create(friendly_name=label, domain_name=domain)

    origination_sid = None
    if origination_uri:
        sip_url = origination_uri if origination_uri.startswith("sip") else f"sip:{origination_uri}"
        existing = trunks(trunk.sid).origination_urls.list()
        match = next((o for o in existing if o.sip_url == sip_url), None)
        if match is None:
            match = trunks(trunk.sid).origination_urls.create(
                weight=10, priority=10, enabled=True, friendly_name=label, sip_url=sip_url
            )
        origination_sid = match.sid

    number_attached = False
    if caller_id:
        owned = client.incoming_phone_numbers.list(phone_number=caller_id, limit=1)
        if owned:
            attached = {p.phone_number for p in trunks(trunk.sid).phone_numbers.list()}
            if caller_id not in attached:
                trunks(trunk.sid).phone_numbers.create(phone_number_sid=owned[0].sid)
            number_attached = True

    return TrunkSyncResult(
        trunk_sid=trunk.sid,
        domain_name=domain,
        origination_sid=origination_sid,
        number_attached=number_attached,
    )


def delete_elastic_sip_trunk(*, trunk_sid: str, credentials: TwilioCredentials | None = None) -> None:
    _client(credentials).trunking.v1.trunks(trunk_sid).delete()


def redirect_live_call(
    *, call_sid: str, twiml: str, credentials: TwilioCredentials | None = None
) -> None:
    redirect_live(client=_client(credentials), call_sid=call_sid, twiml=twiml)
