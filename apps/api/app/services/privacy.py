"""India's DPDP Act 2023 / DPDP Rules 2025 controls for a business using the
platform: the privacy notice, a ledger of consent, data-principal requests with
deadlines, a personal-data-breach register, and a self-check.

Not legal advice. The deadlines here follow the Rules as notified in November
2025; check them against the current text and your own counsel."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.services.customer_lookup import normalize_phone
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    ConsentRecord,
    Customer,
    DataBreach,
    DndEntry,
    PrivacyProfile,
    PrivacyRequest,
    RetentionPolicy,
    Tenant,
    TenantSecuritySettings,
    WidgetLead,
)
from packages.database.models.privacy import (
    DEFAULT_CALL_DISCLOSURE,
    DEFAULT_PURPOSES,
    DEFAULT_RETENTION,
)

REQUEST_KINDS = ("access", "correction", "erasure", "withdraw_consent", "grievance", "nominee")
REQUEST_STATUSES = ("received", "verifying", "in_progress", "completed", "rejected")
CONSENT_PURPOSES = ("enquiry", "call_back", "marketing", "recording")
BREACH_REPORT_HOURS = 72  # Rule 7: detailed report to the Board within 72 hours
# The Rules fix 90 days to answer a grievance (Rule 14); the business sets its own, shorter target.
OUTER_LIMIT_DAYS = 90
ADULT_AGE = 18


class PrivacyError(Exception):
    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def public_url(slug: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}/privacy/{slug}"


def _now() -> datetime:
    return datetime.now(UTC)


# ---- profile and notice ---------------------------------------------------------------------


async def get_profile(db: AsyncSession, tenant_id: uuid.UUID, *, create: bool = True) -> PrivacyProfile | None:
    row = (
        await db.execute(select(PrivacyProfile).where(PrivacyProfile.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None and create:
        tenant = await db.get(Tenant, tenant_id)
        # Several requests can arrive together on a business's first visit (the page asks for the
        # profile from more than one place). ON CONFLICT lets exactly one create the row and the
        # others read it, instead of one of them failing with a 500.
        await db.execute(
            pg_insert(PrivacyProfile)
            .values(
                tenant_id=tenant_id,
                legal_name=tenant.name if tenant else None,
                officer_title="Grievance Officer",
                purposes=list(DEFAULT_PURPOSES),
                retention_summary=DEFAULT_RETENTION,
                call_disclosure_text=DEFAULT_CALL_DISCLOSURE,
                notice_version=1,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        row = (
            await db.execute(select(PrivacyProfile).where(PrivacyProfile.tenant_id == tenant_id))
        ).scalar_one()
    return row


_NOTICE_FIELDS = ("legal_name", "officer_name", "officer_title", "officer_email", "officer_phone",
                  "address", "purposes", "retention_summary", "extra_notice")


async def update_profile(db: AsyncSession, tenant_id: uuid.UUID, changes: dict) -> PrivacyProfile:
    """Applies the edit; if anything people are told changed, the notice version
    goes up so earlier consents stay tied to the wording they were given under."""
    row = await get_profile(db, tenant_id)
    before = {k: getattr(row, k) for k in _NOTICE_FIELDS}
    for key, value in changes.items():
        setattr(row, key, value)
    if any(getattr(row, k) != before[k] for k in _NOTICE_FIELDS):
        row.notice_version += 1
    await db.flush()
    return row


def notice_lines(profile: PrivacyProfile, business: str) -> list[str]:
    name = profile.legal_name or business
    lines = [f"{name} collects your name and contact details and what you tell us here."]
    if profile.purposes:
        lines.append("We use them for: " + "; ".join(p.rstrip(".") for p in profile.purposes) + ".")
    lines.append(profile.retention_summary)
    lines.append(
        "You can see, correct or delete your details, withdraw this consent, or complain, "
        "at any time using the link below. Withdrawing is as easy as agreeing."
    )
    if profile.officer_email or profile.officer_phone:
        contact = " / ".join(x for x in (profile.officer_email, profile.officer_phone) if x)
        lines.append(f"{profile.officer_title}: {profile.officer_name or name}, {contact}.")
    if profile.extra_notice:
        lines.append(profile.extra_notice)
    lines.append(f"By continuing you confirm that you are {ADULT_AGE} or older.")
    return lines


def notice_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


async def public_notice(db: AsyncSession, tenant_id: uuid.UUID, business: str) -> dict:
    profile = await get_profile(db, tenant_id)
    lines = notice_lines(profile, business)
    return {
        "business": profile.legal_name or business,
        "version": profile.notice_version,
        "hash": notice_hash(lines),
        "lines": lines,
        "purposes": list(profile.purposes or []),
        "retention": profile.retention_summary,
        "officer": {
            "name": profile.officer_name,
            "title": profile.officer_title,
            "email": profile.officer_email,
            "phone": profile.officer_phone,
        },
        "address": profile.address,
        "sla_days": profile.request_sla_days,
    }


async def call_disclosure(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """What the caller hears first: that they are talking to an AI and, if calls
    are recorded, that they may be recorded.

    Turning the sentence off is allowed only while recording is off. A recorded
    call always says so, because the recording is the caller's personal data.
    """
    profile = await get_profile(db, tenant_id, create=False)  # read-only: this runs mid-call
    tenant = await db.get(Tenant, tenant_id)
    recording = bool(tenant and tenant.call_recording_enabled)
    if profile is not None and not profile.call_disclosure_enabled and not recording:
        return ""
    if recording and (profile is None or not profile.call_disclosure_enabled):
        return DEFAULT_CALL_DISCLOSURE
    text = (profile.call_disclosure_text if profile else DEFAULT_CALL_DISCLOSURE).strip()
    if text == DEFAULT_CALL_DISCLOSURE and not recording:
        return "This call is with an AI assistant."
    return text


async def has_outbound_consent(db: AsyncSession, tenant_id: uuid.UUID, phone: str | None) -> bool:
    """True when this number agreed to a marketing or callback call and has not taken it back."""
    phone = _clean_phone(phone)
    if not phone:
        return False
    row = await db.execute(
        select(ConsentRecord.id)
        .where(
            ConsentRecord.tenant_id == tenant_id,
            ConsentRecord.subject_phone == phone,
            ConsentRecord.purpose.in_(("marketing", "call_back")),
            ConsentRecord.status == "granted",
        )
        .limit(1)
    )
    return row.first() is not None


# ---- consent ledger -------------------------------------------------------------------------


def _clean_phone(phone: str | None) -> str | None:
    phone = (phone or "").strip()
    return normalize_phone(phone) if phone else None


async def record_consent(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    purposes: list[str],
    source: str,
    business: str,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    customer_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> list[ConsentRecord]:
    """One row per purpose, tied to the notice version and a hash of its words."""
    profile = await get_profile(db, tenant_id)
    lines = notice_lines(profile, business)
    digest = notice_hash(lines)
    phone = _clean_phone(phone)
    email = (email or "").strip().lower() or None
    now = _now()
    rows = []
    for purpose in dict.fromkeys(purposes):
        if purpose not in CONSENT_PURPOSES:
            raise PrivacyError(f"Unknown purpose '{purpose}'")
        row = ConsentRecord(
            tenant_id=tenant_id,
            customer_id=customer_id,
            subject_name=(name or None) and name[:255],
            subject_phone=phone,
            subject_email=email,
            purpose=purpose,
            status="granted",
            source=source,
            notice_version=profile.notice_version,
            notice_hash=digest,
            ip_address=ip,
            user_agent=(user_agent or "")[:255] or None,
            granted_at=now,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    return rows


async def withdraw_consent(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    phone: str | None = None,
    email: str | None = None,
    purposes: list[str] | None = None,
    via: str = "request",
) -> dict[str, int]:
    """Stops using the person's details for the purposes they take back (all of
    them if none are named). A withdrawn call_back/marketing consent puts the
    number on the do-not-disturb list, so no dialler or campaign can reach it."""
    phone = _clean_phone(phone)
    email = (email or "").strip().lower() or None
    if not phone and not email:
        raise PrivacyError("A phone number or email address is needed to find the consent.")
    who = [c for c in (ConsentRecord.subject_phone == phone if phone else None,
                       ConsentRecord.subject_email == email if email else None) if c is not None]
    stmt = update(ConsentRecord).where(
        ConsentRecord.tenant_id == tenant_id,
        ConsentRecord.status == "granted",
        or_(*who),
    )
    if purposes:
        stmt = stmt.where(ConsentRecord.purpose.in_(purposes))
    result = await db.execute(
        stmt.values(status="withdrawn", withdrawn_at=_now(), withdrawn_via=via)
    )
    withdrawn = result.rowcount or 0
    blocked = 0
    if phone and (not purposes or {"call_back", "marketing"} & set(purposes)):
        exists = (
            await db.execute(
                select(DndEntry.id).where(DndEntry.tenant_id == tenant_id, DndEntry.e164_number == phone)
            )
        ).first()
        if not exists:
            db.add(DndEntry(tenant_id=tenant_id, e164_number=phone, source="consent_withdrawn",
                            note="Withdrew consent (DPDP)"))
            blocked = 1
        await db.execute(
            update(WidgetLead)
            .where(WidgetLead.tenant_id == tenant_id, WidgetLead.phone == phone)
            .values(consent_call=False)
        )
    suppressed = 0
    if not purposes or {"call_back", "marketing"} & set(purposes):
        more_numbers, suppressed = await _opt_out_everywhere(db, tenant_id, phone, email, via)
        blocked += more_numbers
    await db.flush()
    return {"consents_withdrawn": withdrawn, "numbers_blocked": blocked, "emails_suppressed": suppressed}


async def _opt_out_everywhere(
    db: AsyncSession, tenant_id: uuid.UUID, phone: str | None, email: str | None, via: str
) -> tuple[int, int]:
    """One opt-out covers every channel: the phone numbers and email addresses on the same customer
    record (and the ones given) all stop getting marketing, whichever channel the person used to say so.
    Returns (numbers newly blocked, emails newly suppressed)."""
    from app.services import email_compliance

    conditions = []
    if phone:
        conditions.append(Customer.phone.in_({phone, phone.lstrip("+")}))
    if email:
        conditions.append(func.lower(Customer.email) == email)
    emails = {email} if email else set()
    phones: set[str] = set()
    if conditions:
        rows = await db.execute(
            select(Customer.phone, Customer.email).where(Customer.tenant_id == tenant_id, or_(*conditions))
        )
        for row_phone, row_email in rows.all():
            if _clean_phone(row_phone):
                phones.add(_clean_phone(row_phone))
            if (row_email or "").strip():
                emails.add(row_email.strip().lower())
    numbers = 0
    for number in phones - {phone}:  # the given number was handled above
        exists = (
            await db.execute(select(DndEntry.id).where(DndEntry.tenant_id == tenant_id, DndEntry.e164_number == number))
        ).first()
        if not exists:
            db.add(DndEntry(tenant_id=tenant_id, e164_number=number, source="opt_out_linked",
                            note="Opted out on another channel"))
            numbers += 1
    suppressed = 0
    for address in emails:
        if email_compliance.normalize_email(address) and await email_compliance.suppress(
            db, tenant_id, address, reason="unsubscribed", source=via[:24]
        ):
            suppressed += 1
    return numbers, suppressed


async def consent_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    rows = (
        await db.execute(
            select(ConsentRecord.purpose, ConsentRecord.status, func.count())
            .where(ConsentRecord.tenant_id == tenant_id)
            .group_by(ConsentRecord.purpose, ConsentRecord.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for purpose, status, n in rows:
        out.setdefault(purpose, {"granted": 0, "withdrawn": 0})[status] = n
    return out


# ---- data-principal requests ----------------------------------------------------------------


def _reference() -> str:
    return "PR-" + secrets.token_hex(4).upper()


async def create_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    name: str,
    email: str | None,
    phone: str | None,
    details: str | None,
    ip: str | None = None,
) -> PrivacyRequest:
    if kind not in REQUEST_KINDS:
        raise PrivacyError("Choose what you would like us to do.")
    name = (name or "").strip()
    if not name:
        raise PrivacyError("Please tell us your name.")
    email = (email or "").strip().lower() or None
    phone = _clean_phone(phone)
    if not email and not phone:
        raise PrivacyError("Please give a phone number or email so we can reach you and find your details.")
    profile = await get_profile(db, tenant_id)
    customer = await find_customer(db, tenant_id, phone, email)
    request = PrivacyRequest(
        tenant_id=tenant_id,
        reference=_reference(),
        kind=kind,
        status="received",
        subject_name=name[:255],
        subject_email=email,
        subject_phone=phone,
        details=(details or "").strip()[:4000] or None,
        customer_id=customer.id if customer else None,
        due_at=_now() + timedelta(days=min(profile.request_sla_days, OUTER_LIMIT_DAYS)),
        ip_address=ip,
    )
    db.add(request)
    await db.flush()
    return request


async def find_customer(db: AsyncSession, tenant_id: uuid.UUID, phone: str | None, email: str | None) -> Customer | None:
    conditions = []
    if phone:
        conditions.append(Customer.phone.in_({phone, phone.lstrip("+")}))
    if email:
        conditions.append(func.lower(Customer.email) == email)
    if not conditions:
        return None
    return (
        await db.execute(
            select(Customer).where(Customer.tenant_id == tenant_id, or_(*conditions)).limit(1)
        )
    ).scalar_one_or_none()


def request_state(row: PrivacyRequest, now: datetime | None = None) -> str:
    """open | due_soon | overdue | done"""
    if row.status in ("completed", "rejected"):
        return "done"
    now = now or _now()
    if row.due_at < now:
        return "overdue"
    if row.due_at - now <= timedelta(days=5):
        return "due_soon"
    return "open"


async def request_by_reference(db: AsyncSession, tenant_id: uuid.UUID, reference: str) -> PrivacyRequest | None:
    return (
        await db.execute(
            select(PrivacyRequest).where(
                PrivacyRequest.tenant_id == tenant_id, PrivacyRequest.reference == reference.strip().upper()
            )
        )
    ).scalar_one_or_none()


# ---- breach register -----------------------------------------------------------------------


async def log_breach(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    title: str,
    description: str,
    discovered_at: datetime | None = None,
    occurred_at: datetime | None = None,
    data_categories: list[str] | None = None,
    approx_people: int | None = None,
    likely_impact: str | None = None,
    mitigation: str | None = None,
    user_id: uuid.UUID | None = None,
) -> DataBreach:
    found = discovered_at or _now()
    row = DataBreach(
        tenant_id=tenant_id,
        title=title.strip()[:200],
        description=description.strip(),
        discovered_at=found,
        occurred_at=occurred_at,
        data_categories=data_categories or [],
        approx_people=approx_people,
        likely_impact=likely_impact,
        mitigation=mitigation,
        status="open",
        report_due_at=found + timedelta(hours=BREACH_REPORT_HOURS),
        created_by_user_id=user_id,
    )
    db.add(row)
    await db.flush()
    return row


def breach_state(row: DataBreach, now: datetime | None = None) -> str:
    if row.status == "closed":
        return "closed"
    if row.board_notified_at:
        return "reported"
    now = now or _now()
    if row.report_due_at < now:
        return "overdue"
    return "report_due"


def breach_notice_draft(row: DataBreach, profile: PrivacyProfile, business: str) -> dict:
    """Plain-language text the owner can send to the people affected (Rule 7(1)),
    and the points the report to the Data Protection Board must cover (Rule 7(2))."""
    name = profile.legal_name or business
    contact = " / ".join(x for x in (profile.officer_email, profile.officer_phone) if x) or "our team"
    categories = ", ".join(row.data_categories) if row.data_categories else "some of your personal details"
    to_people = (
        f"Subject: A security incident involving your information at {name}\n\n"
        f"We are writing to tell you about an incident affecting {categories}. "
        f"{row.description.strip()}\n\n"
        f"What this could mean for you: {row.likely_impact or 'we are still assessing the impact.'}\n"
        f"What we have done: {row.mitigation or 'we are investigating and securing our systems.'}\n"
        "What you can do: be careful with unexpected calls, messages and emails that mention us, "
        "and do not share one-time passwords.\n"
        f"If you have questions, contact {profile.officer_name or profile.officer_title} at {contact}."
    )
    to_board = {
        "nature_extent_timing_location": {
            "what_happened": row.description,
            "discovered_at": row.discovered_at.isoformat(),
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            "data_categories": row.data_categories,
            "approx_people": row.approx_people,
        },
        "likely_impact": row.likely_impact,
        "mitigation_steps": row.mitigation,
        "findings_on_cause": None,
        "steps_to_prevent_recurrence": None,
        "people_notified": row.principals_notified_how,
        "report_due_at": row.report_due_at.isoformat(),
    }
    return {"to_people": to_people, "to_board": to_board}


# ---- self-check -----------------------------------------------------------------------------


async def compliance_checklist(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """What the software can show is done, and what only the business can do.
    Each item says where to fix it."""
    tenant = await db.get(Tenant, tenant_id)
    profile = await get_profile(db, tenant_id)
    retention = (
        await db.execute(select(RetentionPolicy).where(RetentionPolicy.tenant_id == tenant_id))
    ).scalar_one_or_none()
    security = (
        await db.execute(select(TenantSecuritySettings).where(TenantSecuritySettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    now = _now()
    overdue_requests = (
        await db.execute(
            select(func.count()).select_from(PrivacyRequest).where(
                PrivacyRequest.tenant_id == tenant_id,
                PrivacyRequest.status.in_(("received", "verifying", "in_progress")),
                PrivacyRequest.due_at < now,
            )
        )
    ).scalar_one()
    overdue_breaches = (
        await db.execute(
            select(func.count()).select_from(DataBreach).where(
                DataBreach.tenant_id == tenant_id,
                DataBreach.status == "open",
                DataBreach.board_notified_at.is_(None),
                DataBreach.report_due_at < now,
            )
        )
    ).scalar_one()
    has_retention = bool(retention and retention.enabled)
    items = [
        ("officer", "Grievance officer named", bool(profile.officer_email or profile.officer_phone),
         "Add a contact so people know who to ask (Rule 9).", "settings/privacy"),
        ("notice", "Privacy notice shows what you collect and why",
         bool(profile.purposes) and bool(profile.retention_summary), "List your purposes and retention (Rule 3).", "settings/privacy"),
        ("legal_name", "Legal name and address given", bool(profile.legal_name and profile.address),
         "Say who is responsible for the data.", "settings/privacy"),
        ("retention", "Automatic deletion is on", has_retention,
         "Turn on retention so old transcripts, recordings and leads are deleted (Rule 8).", "settings/security"),
        ("recording_notice", "Callers are told when a call is AI and recorded",
         profile.call_disclosure_enabled or not (tenant and tenant.call_recording_enabled),
         "Keep the call disclosure on.", "settings/privacy"),
        ("pii_masking", "Personal data hidden from staff who do not need it",
         bool(security and security.mask_pii), "Turn on masking in security settings (Rule 6).", "settings/security"),
        ("requests_on_time", "No privacy request is overdue", overdue_requests == 0,
         f"{overdue_requests} overdue." if overdue_requests else "", "settings/privacy/requests"),
        ("breaches_on_time", "No breach report is overdue", overdue_breaches == 0,
         f"{overdue_breaches} overdue." if overdue_breaches else "", "settings/privacy/breaches"),
    ]
    return {
        "items": [
            {"key": k, "label": label, "ok": bool(ok), "hint": hint, "href": href}
            for k, label, ok, hint, href in items
        ],
        "score": sum(1 for it in items if it[2]),
        "total": len(items),
    }


async def erase_by_contact(
    db: AsyncSession, tenant_id: uuid.UUID, phone: str | None, email: str | None
) -> dict[str, int]:
    """Erasure for someone who is not stored as a customer: delete their website
    chat enquiries and take their identity off the consent proof."""
    phone = _clean_phone(phone)
    email = (email or "").strip().lower() or None
    leads = [c for c in (WidgetLead.phone == phone if phone else None,
                         func.lower(WidgetLead.email) == email if email else None) if c is not None]
    consents = [c for c in (ConsentRecord.subject_phone == phone if phone else None,
                            ConsentRecord.subject_email == email if email else None) if c is not None]
    counts = {"website_chat_enquiries": 0, "consents_anonymised": 0}
    if leads:
        counts["website_chat_enquiries"] = (
            await db.execute(delete(WidgetLead).where(WidgetLead.tenant_id == tenant_id, or_(*leads)))
        ).rowcount or 0
    if consents:
        counts["consents_anonymised"] = (
            await db.execute(
                update(ConsentRecord)
                .where(ConsentRecord.tenant_id == tenant_id, or_(*consents))
                .values(subject_name=None, subject_phone=None, subject_email=None, customer_id=None)
            )
        ).rowcount or 0
    return counts
