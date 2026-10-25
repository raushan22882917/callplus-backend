"""Moving every client's phone numbers to the platform's current address."""

import uuid

import pytest
from app.core.config import get_settings
from app.integrations import twilio_client
from app.integrations.credentials import TwilioCredentials
from app.services import number_webhooks as nw
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import PhoneNumber, Tenant, User
from packages.database.models.platform import PlatformAdmin
from packages.database.session import session_scope

NEW = "https://api.new.example"
OLD = "https://api.old.example"
VOICE = "/api/v1/webhooks/telephony/twilio/voice"
STATUS = "/api/v1/webhooks/telephony/twilio/status"


# ---- deciding what to change ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field, current, expected",
    [
        ("voice_url", f"{OLD}{VOICE}", f"{NEW}{VOICE}"),
        ("voice_url", f"{OLD}{VOICE}/", f"{NEW}{VOICE}/"),  # a trailing slash is still ours
        ("voice_url", f"{OLD}{VOICE}?x=1", f"{NEW}{VOICE}?x=1"),
        ("status_callback", f"{OLD}{STATUS}", f"{NEW}{STATUS}"),
        ("voice_url", f"{NEW}{VOICE}", None),  # already right
        ("voice_url", "", None),
        ("voice_url", None, None),
        ("sms_url", "https://api.vapi.ai/twilio/sms", None),  # another service's address
        ("voice_url", "https://example.org/my-own-ivr", None),
        ("voice_url", f"{OLD}/api/v1/something/else", None),
    ],
)
def test_only_our_own_webhooks_on_another_host_are_moved(field, current, expected):
    assert nw.retarget(field, current, NEW) == expected


# ---- the whole sweep ------------------------------------------------------------------------


class FakeTwilio:
    """Each Twilio account holds numbers by sid; remembers who asked."""

    def __init__(self):
        self.numbers: dict[tuple[str, str], dict] = {}
        self.updates: list[tuple[str, str, dict]] = []
        self.broken_accounts: set[str] = set()

    def find(self, *, phone_number, credentials=None):
        for (account, sid), hooks in self.numbers.items():
            if account == credentials.account_sid and hooks.get("_number") == phone_number:
                return sid
        return None

    def fetch(self, *, provider_sid, credentials=None):
        if credentials.account_sid in self.broken_accounts:
            raise RuntimeError("authentication failed")
        return {k: v for k, v in self.numbers[(credentials.account_sid, provider_sid)].items() if k != "_number"}

    def update(self, *, provider_sid, changes, credentials=None):
        self.updates.append((credentials.account_sid, provider_sid, dict(changes)))
        self.numbers[(credentials.account_sid, provider_sid)].update(changes)


@pytest.fixture
def twilio(monkeypatch):
    fake = FakeTwilio()
    monkeypatch.setattr(twilio_client, "find_number_sid", fake.find)
    monkeypatch.setattr(twilio_client, "fetch_number_webhooks", fake.fetch)
    monkeypatch.setattr(twilio_client, "update_number_webhooks", fake.update)
    monkeypatch.setattr(get_settings(), "public_base_url", NEW)

    async def creds(db, tenant_id):
        tenant = await db.get(Tenant, tenant_id)
        # clients that connected their own Twilio account use it; the rest use the platform's
        return TwilioCredentials(account_sid=f"AC_{tenant.slug}" if tenant.slug.startswith("own-") else "AC_platform", auth_token="t")

    monkeypatch.setattr(nw, "get_twilio_credentials", creds)
    return fake


TOKENS: dict[str, str] = {}


async def _tenant(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post("/api/v1/auth/signup", json={
        "company_name": slug, "tenant_slug": slug, "full_name": "Owner", "email": f"o@{slug}.com", "password": "supersecret1"})
    assert resp.status_code == 201, resp.text
    TOKENS[slug] = resp.json()["access_token"]
    async with session_scope() as db:
        return (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()


async def _number(tenant_id, e164, sid, fake, account, **hooks):
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=tenant_id, e164_number=e164, provider_sid=sid))
        await db.commit()
    fake.numbers[(account, sid)] = {"voice_url": f"{OLD}{VOICE}", "status_callback": None, "sms_url": "", **hooks}


async def _sync(apply: bool, **kw):
    async with session_scope() as db:
        report = await nw.sync_number_webhooks(db, apply=apply, **kw)
        await db.commit()
        return report


async def test_a_dry_run_reports_every_clients_numbers_and_changes_nothing(client: AsyncClient, twilio):
    a, b = await _tenant(client, "acme"), await _tenant(client, "own-bakery")
    await _number(a, "+911", "PN1", twilio, "AC_platform")
    await _number(b, "+912", "PN2", twilio, "AC_own-bakery")
    report = await _sync(apply=False)
    assert [(r["tenant"], r["number"], r["status"]) for r in report] == [
        ("acme", "+911", "would update"), ("own-bakery", "+912", "would update")]
    assert report[0]["changes"]["voice_url"] == {"from": f"{OLD}{VOICE}", "to": f"{NEW}{VOICE}"}
    assert twilio.updates == []


async def test_applying_moves_ours_through_each_clients_own_account_and_leaves_the_rest(client: AsyncClient, twilio):
    a, b = await _tenant(client, "acme"), await _tenant(client, "own-bakery")
    await _number(a, "+911", "PN1", twilio, "AC_platform",
                  status_callback=f"{OLD}{STATUS}", sms_url="https://api.vapi.ai/twilio/sms")
    await _number(b, "+912", "PN2", twilio, "AC_own-bakery")
    report = await _sync(apply=True)
    assert {r["status"] for r in report} == {"updated"}
    assert twilio.numbers[("AC_platform", "PN1")] == {
        "voice_url": f"{NEW}{VOICE}", "status_callback": f"{NEW}{STATUS}", "sms_url": "https://api.vapi.ai/twilio/sms"}
    assert twilio.numbers[("AC_own-bakery", "PN2")]["voice_url"] == f"{NEW}{VOICE}"
    assert {u[0] for u in twilio.updates} == {"AC_platform", "AC_own-bakery"}  # each client's own account
    assert all("sms_url" not in u[2] for u in twilio.updates)  # another service's address never touched
    again = await _sync(apply=True)  # nothing left to do
    assert {r["status"] for r in again} == {"already correct"} and len(twilio.updates) == 2


async def test_one_clients_failure_does_not_stop_the_others(client: AsyncClient, twilio):
    a, b = await _tenant(client, "acme"), await _tenant(client, "own-broken")
    await _number(a, "+911", "PN1", twilio, "AC_platform")
    await _number(b, "+912", "PN2", twilio, "AC_own-broken")
    twilio.broken_accounts.add("AC_own-broken")
    report = {r["number"]: r for r in await _sync(apply=True)}
    assert report["+911"]["status"] == "updated"
    assert report["+912"]["status"] == "failed" and "authentication failed" in report["+912"]["error"]


async def test_non_twilio_numbers_are_skipped_and_one_client_can_be_synced_alone(client: AsyncClient, twilio):
    a, b = await _tenant(client, "acme"), await _tenant(client, "other")
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=a, e164_number="+913", provider="sip", provider_sid=None))
        await db.commit()
    await _number(b, "+914", "PN4", twilio, "AC_platform")
    only_a = await _sync(apply=True, tenant_id=a)
    assert [(r["number"], r["status"]) for r in only_a] == [("+913", "skipped")]
    assert twilio.updates == []  # the other client was not touched


async def test_a_number_registered_by_hand_is_found_by_its_number_and_remembered(client: AsyncClient, twilio):
    tenant = await _tenant(client, "acme")
    async with session_scope() as db:
        db.add(PhoneNumber(tenant_id=tenant, e164_number="+915", provider_sid=None))
        db.add(PhoneNumber(tenant_id=tenant, e164_number="+916", provider_sid=None))
        await db.commit()
    twilio.numbers[("AC_platform", "PN5")] = {"_number": "+915", "voice_url": f"{OLD}{VOICE}", "status_callback": None, "sms_url": ""}
    dry = {r["number"]: r["status"] for r in await _sync(apply=False)}
    assert dry == {"+915": "would update", "+916": "skipped"}
    async with session_scope() as db:
        assert (await db.execute(select(PhoneNumber.provider_sid).where(PhoneNumber.e164_number == "+915"))).scalar_one() is None
    done = {r["number"]: r for r in await _sync(apply=True)}
    assert done["+915"]["status"] == "updated" and "not found" in done["+916"]["error"]
    assert twilio.numbers[("AC_platform", "PN5")]["voice_url"] == f"{NEW}{VOICE}"
    async with session_scope() as db:
        assert (await db.execute(select(PhoneNumber.provider_sid).where(PhoneNumber.e164_number == "+915"))).scalar_one() == "PN5"


# ---- the admin endpoint ---------------------------------------------------------------------


async def test_only_platform_admins_can_run_it_and_the_default_changes_nothing(client: AsyncClient, twilio):
    tenant = await _tenant(client, "acme")
    await _number(tenant, "+911", "PN1", twilio, "AC_platform")
    headers = {"Authorization": f"Bearer {TOKENS['acme']}"}
    assert (await client.post("/api/v1/platform/admin/numbers/sync-webhooks", headers=headers)).status_code == 403
    assert (await client.post("/api/v1/platform/admin/numbers/sync-webhooks")).status_code == 401

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == "o@acme.com"))).scalar_one()
        db.add(PlatformAdmin(user_id=user.id))
        await db.commit()
    dry = await client.post("/api/v1/platform/admin/numbers/sync-webhooks", headers=headers)
    assert dry.status_code == 200
    assert dry.json()["applied"] is False and dry.json()["target"] == NEW and dry.json()["counts"] == {"would update": 1}
    assert twilio.updates == []
    done = await client.post("/api/v1/platform/admin/numbers/sync-webhooks?apply=true", headers=headers)
    assert done.json()["counts"] == {"updated": 1} and len(twilio.updates) == 1
