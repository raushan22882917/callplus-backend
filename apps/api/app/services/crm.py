"""CRM business logic: lead scoring, pipeline grouping, CSV import, and
tenant-ownership checks for cross-references. Everything takes an explicit
tenant_id; nothing here reads a tenant from user input."""

import csv
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.core.exceptions import NotFoundError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.call import Call
from packages.database.models.crm import (
    CRM_STAGE_ORDER,
    CrmCompany,
    CrmLead,
    CrmNote,
    CrmStage,
    CrmTask,
    CrmTaskStatus,
)
from packages.database.models.customer import Customer
from packages.database.models.rbac import TenantUser

MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 2 * 1024 * 1024

# ---------------------------------------------------------------- ownership


async def ensure_refs(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    company_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    deal_id: uuid.UUID | None = None,
) -> None:
    """404 if any referenced row does not belong to this tenant."""
    from packages.database.models.crm import CrmDeal

    checks = [
        (company_id, CrmCompany, "Company"),
        (customer_id, Customer, "Customer"),
        (lead_id, CrmLead, "Lead"),
        (deal_id, CrmDeal, "Deal"),
    ]
    for ref, model, label in checks:
        if ref is None:
            continue
        found = await db.execute(
            select(model.id).where(model.id == ref, model.tenant_id == tenant_id)
        )
        if found.scalar_one_or_none() is None:
            raise NotFoundError(f"{label} not found")
    if owner_user_id is not None:
        found = await db.execute(
            select(TenantUser.id).where(
                TenantUser.user_id == owner_user_id, TenantUser.tenant_id == tenant_id
            )
        )
        if found.first() is None:
            raise NotFoundError("User not found")


# ------------------------------------------------------------------ scoring

SOURCE_POINTS = {
    "referral": 15,
    "inbound_call": 12,
    "ai_call": 12,
    "whatsapp": 10,
    "website": 10,
    "campaign": 5,
    "import": 3,
    "manual": 3,
}
STAGE_POINTS = {
    CrmStage.NEW_LEAD: 0,
    CrmStage.CONTACTED: 3,
    CrmStage.QUALIFIED: 8,
    CrmStage.DEMO: 12,
    CrmStage.PROPOSAL: 16,
    CrmStage.NEGOTIATION: 20,
}
BUY_INTENT = re.compile(r"buy|purchase|pric|quote|demo|plan|order|subscri|interested|sign ?up", re.I)
POSITIVE_OUTCOME = re.compile(r"interested|book|appoint|sale|convert|qualified|resolved|callback", re.I)
NEGATIVE_OUTCOME = re.compile(r"not[ _-]?interested|declin|spam|wrong|reject|do[ _-]?not[ _-]?call", re.I)


@dataclass
class ScoreSignals:
    stage: CrmStage = CrmStage.NEW_LEAD
    source: str | None = None
    has_email: bool = False
    has_phone: bool = False
    # (sentiment, outcome, intent) per analyzed call, newest first
    calls: list[tuple[str | None, str | None, str | None]] = field(default_factory=list)
    total_calls: int = 0
    notes: int = 0
    tasks_done: int = 0


@dataclass
class ScoreResult:
    score: int
    breakdown: list[dict]


def compute_score(s: ScoreSignals) -> ScoreResult:
    """Pure, rule-based 0-100 score. Won=100 and Lost=0 are terminal."""
    if s.stage == CrmStage.WON:
        return ScoreResult(100, [{"rule": "stage", "points": 100, "detail": "Won"}])
    if s.stage == CrmStage.LOST:
        return ScoreResult(0, [{"rule": "stage", "points": 0, "detail": "Lost"}])

    items: list[dict] = [{"rule": "base", "points": 10, "detail": "Baseline"}]

    src = (s.source or "").strip().lower()
    if src:
        items.append(
            {"rule": "source", "points": SOURCE_POINTS.get(src, 4), "detail": f"Source: {src}"}
        )

    contact = (2 if s.has_email else 0) + (2 if s.has_phone else 0)
    if contact:
        items.append({"rule": "contact", "points": contact, "detail": "Reachable contact details"})

    call_points = 0
    intent_awarded = False
    outcome_awarded = False
    for sentiment, outcome, intent in s.calls[:5]:
        sent = (sentiment or "").lower()
        if sent == "positive":
            call_points += 8
        elif sent == "neutral":
            call_points += 2
        elif sent == "negative":
            call_points -= 6
        if outcome and not outcome_awarded:
            if NEGATIVE_OUTCOME.search(outcome):
                call_points -= 15
                outcome_awarded = True
            elif POSITIVE_OUTCOME.search(outcome):
                call_points += 8
                outcome_awarded = True
        if intent and not intent_awarded and BUY_INTENT.search(intent):
            call_points += 10
            intent_awarded = True
    call_points = max(-30, min(45, call_points))
    if s.calls:
        items.append(
            {
                "rule": "calls",
                "points": call_points,
                "detail": f"Sentiment/outcome/intent across {min(len(s.calls), 5)} analyzed call(s)",
            }
        )

    engagement = min(s.total_calls, 5) * 3 + min(s.notes, 3) * 2 + min(s.tasks_done, 3) * 2
    if engagement:
        items.append(
            {
                "rule": "engagement",
                "points": engagement,
                "detail": f"{s.total_calls} call(s), {s.notes} note(s), {s.tasks_done} task(s) done",
            }
        )

    stage_pts = STAGE_POINTS.get(s.stage, 0)
    if stage_pts:
        items.append({"rule": "stage", "points": stage_pts, "detail": f"Stage: {s.stage.value}"})

    total = sum(i["points"] for i in items)
    return ScoreResult(max(0, min(100, total)), items)


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def _phone_variants(phone: str | None) -> list[str]:
    raw = (phone or "").strip()
    if not _digits(raw):
        return []
    digits = _digits(raw)
    return list({raw, digits, f"+{digits}"})


async def calls_for_lead(db: AsyncSession, lead: CrmLead, *, limit: int = 50) -> list[Call]:
    conds = []
    if lead.customer_id:
        conds.append(Call.customer_id == lead.customer_id)
    if lead.source_call_id:
        conds.append(Call.id == lead.source_call_id)
    variants = _phone_variants(lead.phone)
    if variants:
        conds.append(Call.from_number.in_(variants))
        conds.append(Call.to_number.in_(variants))
    if not conds:
        return []
    result = await db.execute(
        select(Call)
        .where(Call.tenant_id == lead.tenant_id, or_(*conds))
        .order_by(Call.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def build_signals(db: AsyncSession, lead: CrmLead) -> ScoreSignals:
    calls = await calls_for_lead(db, lead)
    notes = await db.scalar(
        select(func.count())
        .select_from(CrmNote)
        .where(CrmNote.tenant_id == lead.tenant_id, CrmNote.lead_id == lead.id, CrmNote.kind == "note")
    )
    done = await db.scalar(
        select(func.count())
        .select_from(CrmTask)
        .where(
            CrmTask.tenant_id == lead.tenant_id,
            CrmTask.lead_id == lead.id,
            CrmTask.status == CrmTaskStatus.DONE,
        )
    )
    return ScoreSignals(
        stage=lead.stage,
        source=lead.source,
        has_email=bool(lead.email),
        has_phone=bool(lead.phone),
        calls=[(c.sentiment, c.outcome, c.intent) for c in calls if c.analyzed_at is not None],
        total_calls=len(calls),
        notes=notes or 0,
        tasks_done=done or 0,
    )


async def rescore_lead(db: AsyncSession, lead: CrmLead) -> CrmLead:
    result = compute_score(await build_signals(db, lead))
    lead.score = result.score
    lead.score_breakdown = result.breakdown
    lead.scored_at = datetime.now(UTC)
    return lead


async def rescore_all(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    leads = (
        (await db.execute(select(CrmLead).where(CrmLead.tenant_id == tenant_id))).scalars().all()
    )
    for lead in leads:
        await rescore_lead(db, lead)
    return len(leads)


# ----------------------------------------------------------------- pipeline


async def move_lead(
    db: AsyncSession,
    lead: CrmLead,
    stage: CrmStage,
    *,
    lost_reason: str | None = None,
    author_user_id: uuid.UUID | None = None,
) -> CrmLead:
    if stage == lead.stage:
        return lead
    previous = lead.stage
    lead.stage = stage
    lead.stage_changed_at = datetime.now(UTC)
    lead.lost_reason = lost_reason if stage == CrmStage.LOST else None
    db.add(
        CrmNote(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            author_user_id=author_user_id,
            kind="stage_change",
            body=f"Moved from {previous.value} to {stage.value}"
            + (f" ({lost_reason})" if stage == CrmStage.LOST and lost_reason else ""),
        )
    )
    await rescore_lead(db, lead)
    return lead


async def pipeline(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    per_stage_limit: int = 50,
    owner_user_id: uuid.UUID | None = None,
    q: str | None = None,
) -> list[dict]:
    """Leads grouped by stage (all eight stages, in board order). count and
    total_value are exact; `leads` holds the top `per_stage_limit` by score."""
    filters = [CrmLead.tenant_id == tenant_id]
    if owner_user_id:
        filters.append(CrmLead.owner_user_id == owner_user_id)
    if q:
        like = f"%{q.strip()}%"
        filters.append(
            or_(CrmLead.name.ilike(like), CrmLead.email.ilike(like), CrmLead.phone.ilike(like))
        )
    agg = await db.execute(
        select(CrmLead.stage, func.count(), func.coalesce(func.sum(CrmLead.value), 0))
        .where(*filters)
        .group_by(CrmLead.stage)
    )
    totals = {row[0]: (row[1], Decimal(row[2])) for row in agg.all()}

    out = []
    for stage in CRM_STAGE_ORDER:
        count, value = totals.get(stage, (0, Decimal(0)))
        leads: list[CrmLead] = []
        if count:
            res = await db.execute(
                select(CrmLead)
                .where(*filters, CrmLead.stage == stage)
                .order_by(CrmLead.score.desc(), CrmLead.created_at.desc())
                .limit(per_stage_limit)
            )
            leads = list(res.scalars().all())
        out.append({"stage": stage, "count": count, "total_value": value, "leads": leads})
    return out


# ---------------------------------------------------------------------- CSV

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{6,20}$")
_HEADER_ALIASES = {
    "full_name": "name",
    "lead": "name",
    "e-mail": "email",
    "mobile": "phone",
    "phone_number": "phone",
    "organization": "company",
    "organisation": "company",
    "company_name": "company",
    "deal_value": "value",
    "amount": "value",
}


def normalize_phone(phone: str) -> str:
    digits = _digits(phone)
    return f"+{digits}" if phone.strip().startswith("+") else digits


async def import_leads_csv(
    db: AsyncSession, tenant_id: uuid.UUID, raw: bytes, *, default_source: str = "import"
) -> tuple[int, list[dict]]:
    """Validate and insert leads from CSV. Columns: name (required), email,
    phone, company, source, tags (; or | separated), stage, value, currency.
    Rows without a valid name/contact, with a duplicate email/phone (within the
    file or already in this tenant) or an unknown stage are skipped with a
    reason; valid rows are still created."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("File must be UTF-8 encoded CSV") from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is empty")
    headers = {}
    for h in reader.fieldnames:
        key = (h or "").strip().lower().replace(" ", "_")
        headers[h] = _HEADER_ALIASES.get(key, key)
    if "name" not in headers.values():
        raise ValueError("CSV must have a 'name' column")

    existing = await db.execute(
        select(CrmLead.email, CrmLead.phone).where(CrmLead.tenant_id == tenant_id)
    )
    seen_emails: set[str] = set()
    seen_phones: set[str] = set()
    for email, phone in existing.all():
        if email:
            seen_emails.add(email.lower())
        if phone:
            seen_phones.add(normalize_phone(phone))

    companies: dict[str, uuid.UUID] = {}
    comp_rows = await db.execute(
        select(CrmCompany.id, CrmCompany.name).where(CrmCompany.tenant_id == tenant_id)
    )
    for cid, cname in comp_rows.all():
        companies.setdefault(cname.strip().lower(), cid)

    created = 0
    skipped: list[dict] = []
    stages = {s.value: s for s in CrmStage}
    for idx, rec in enumerate(reader, start=2):  # row 1 is the header
        if idx - 1 > MAX_IMPORT_ROWS:
            skipped.append({"row": idx, "reason": f"Row limit of {MAX_IMPORT_ROWS} exceeded"})
            break
        row = {headers[k]: (v or "").strip() for k, v in rec.items() if k in headers}
        if not any(row.values()):
            continue
        name, email, phone = row.get("name", ""), row.get("email", ""), row.get("phone", "")
        if not name:
            skipped.append({"row": idx, "reason": "Missing name"})
            continue
        if len(name) > 255:
            skipped.append({"row": idx, "reason": "Name too long"})
            continue
        if email and not _EMAIL_RE.match(email):
            skipped.append({"row": idx, "reason": f"Invalid email: {email}"})
            continue
        if phone and not _PHONE_RE.match(phone):
            skipped.append({"row": idx, "reason": f"Invalid phone: {phone}"})
            continue
        if not email and not phone:
            skipped.append({"row": idx, "reason": "Needs an email or phone"})
            continue
        stage_raw = row.get("stage", "").lower().replace(" ", "_")
        if stage_raw and stage_raw not in stages:
            skipped.append({"row": idx, "reason": f"Unknown stage: {row['stage']}"})
            continue
        value = Decimal(0)
        if row.get("value"):
            try:
                value = Decimal(row["value"].replace(",", ""))
                if value < 0 or value >= Decimal(10) ** 12:
                    raise InvalidOperation
            except InvalidOperation:
                skipped.append({"row": idx, "reason": f"Invalid value: {row['value']}"})
                continue
        e_key = email.lower()
        p_key = normalize_phone(phone) if phone else ""
        if e_key and e_key in seen_emails:
            skipped.append({"row": idx, "reason": f"Duplicate email: {email}"})
            continue
        if p_key and p_key in seen_phones:
            skipped.append({"row": idx, "reason": f"Duplicate phone: {phone}"})
            continue

        company_id = None
        if row.get("company"):
            ckey = row["company"].lower()
            company_id = companies.get(ckey)
            if company_id is None:
                company = CrmCompany(tenant_id=tenant_id, name=row["company"][:255])
                db.add(company)
                await db.flush()
                company_id = companies[ckey] = company.id

        tags = [t.strip() for t in re.split(r"[;|]", row.get("tags", "")) if t.strip()]
        currency = (row.get("currency") or "INR").upper()[:3]
        lead = CrmLead(
            tenant_id=tenant_id,
            name=name,
            email=email or None,
            phone=phone or None,
            company_id=company_id,
            stage=stages[stage_raw] if stage_raw else CrmStage.NEW_LEAD,
            source=(row.get("source") or default_source)[:80],
            tags=tags,
            value=value,
            currency=currency,
        )
        db.add(lead)
        await db.flush()
        await rescore_lead(db, lead)
        if e_key:
            seen_emails.add(e_key)
        if p_key:
            seen_phones.add(p_key)
        created += 1
    return created, skipped


async def push_lead_to_connected_crms(db: AsyncSession, lead: CrmLead) -> None:
    """Best-effort upsert of a freshly created lead into the tenant's
    connected HubSpot / Zoho / Salesforce (per-tenant OAuth). Never raises."""
    from app.services import crm_push

    await crm_push.push_lead_safely(db, lead)
