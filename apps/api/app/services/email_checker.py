"""A plain check of an email before it goes out: will it land in the inbox, and will anyone read it.

It is arithmetic on the text, so the same email always gets the same score and every point lost has a stated
reason. It does not replace a deliverability test; it catches what makes mail providers and readers suspicious.
"""

import re

SPAMMY = (
    "free", "winner", "guarantee", "guaranteed", "act now", "urgent", "limited time", "click here", "100%", "cash",
    "earn money", "buy now", "no obligation", "risk-free", "congratulations", "you have been selected", "double your",
    "make money", "order now", "exclusive deal", "$$$",
)
SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co/", "ow.ly", "is.gd", "buff.ly", "cutt.ly")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_TOKEN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
KNOWN_TOKENS = {"name", "first_name", "email", "phone"}
_BRACKETED = re.compile(r"\[[^\]\n]{2,60}\]")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def _issue(level: str, field: str, text: str, points: int) -> dict:
    return {"level": level, "field": field, "text": text, "points": points}


def check_email(subject: str, body: str) -> dict:
    subject = (subject or "").strip()
    body = (body or "").strip()
    issues: list[dict] = []
    lowered = f"{subject}\n{body}".lower()

    # Subject
    if not subject:
        issues.append(_issue("bad", "subject", "Add a subject. An email without one is often filtered.", 25))
    else:
        if len(subject) > 80:
            issues.append(_issue("bad", "subject", f"The subject is {len(subject)} characters. Phones show about 40; keep it under 60.", 12))
        elif len(subject) > 60:
            issues.append(_issue("warn", "subject", f"The subject is {len(subject)} characters. Under 60 is safer.", 5))
        elif len(subject) < 8:
            issues.append(_issue("warn", "subject", "The subject is very short. Say what the email is about.", 4))
        letters = [c for c in subject if c.isalpha()]
        if len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            issues.append(_issue("bad", "subject", "The subject is mostly capitals. It reads like shouting and trips spam filters.", 12))
        if subject.count("!") > 1:
            issues.append(_issue("warn", "subject", "More than one exclamation mark in the subject.", 6))
        if len(_EMOJI.findall(subject)) > 2:
            issues.append(_issue("warn", "subject", "Several emoji in the subject. One is plenty.", 4))
        if re.match(r"^(re|fwd?):", subject, re.IGNORECASE):
            issues.append(_issue("bad", "subject", 'Starting with "Re:" or "Fwd:" pretends it is a reply. Providers penalise that.', 15))

    # Body
    words = len(re.findall(r"\w+", body))
    if not body:
        issues.append(_issue("bad", "body", "Add a message.", 30))
    else:
        if words < 15:
            issues.append(_issue("warn", "body", "The message is very short. A line or two of context reads as more trustworthy.", 6))
        if words > 300:
            issues.append(_issue("warn", "body", f"The message is {words} words. Most readers stop after about 150.", 6))
        letters = [c for c in body if c.isalpha()]
        if len(letters) >= 40 and sum(c.isupper() for c in letters) / len(letters) > 0.3:
            issues.append(_issue("bad", "body", "A lot of the message is in capitals.", 10))
        if body.count("!") > 3:
            issues.append(_issue("warn", "body", "Many exclamation marks.", 5))
        links = _URL.findall(body)
        if len(links) > 3:
            issues.append(_issue("warn", "body", f"{len(links)} links. More than three raises spam risk; keep one clear action.", 6))
        if any(s in link.lower() for link in links for s in SHORTENERS):
            issues.append(_issue("bad", "body", "A shortened link (bit.ly and similar) is often blocked. Use the full address.", 12))
        if not re.search(r"\{\{\s*(first_)?name\s*\}\}", body, re.IGNORECASE):
            issues.append(_issue("tip", "body", "Greet each person by name with {{first_name}}. Personal email gets read more.", 3))

    leftover = _BRACKETED.findall(f"{subject}\n{body}")
    if leftover:
        issues.append(_issue("bad", "body", "Replace the [bracketed] parts before sending: " + ", ".join(leftover[:3]) + ".", 15))

    unknown = sorted({t.lower() for t in _TOKEN.findall(f"{subject} {body}") if t.lower() not in KNOWN_TOKENS})
    if unknown:
        issues.append(_issue("bad", "body", "These blanks will be left empty: " + ", ".join("{{" + t + "}}" for t in unknown) + ". Use name, first_name, email or phone.", 10))

    hits = sorted({w for w in SPAMMY if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", lowered)})
    if hits:
        issues.append(_issue("warn" if len(hits) < 3 else "bad", "body", "Spam-filter words: " + ", ".join(hits[:6]) + ".", min(20, 5 * len(hits))))

    score = max(0, 100 - sum(i["points"] for i in issues))
    return {
        "score": score,
        "label": "good" if score >= 80 else ("fair" if score >= 60 else "risky"),
        "issues": [{k: v for k, v in i.items() if k != "points"} for i in issues],
        "stats": {"subject_length": len(subject), "words": words, "links": len(_URL.findall(body))},
    }
