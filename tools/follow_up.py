"""Evidence-backed sent follow-ups, independent of employer replies."""

from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def follow_up_fields(sent_date: str, evidence: str) -> dict:
    sent_date, evidence = sent_date.strip(), evidence.strip()
    if not sent_date and not evidence:
        return {}
    if not sent_date or not evidence:
        raise ValueError("A sent follow-up requires both FOLLOW_UP_DATE and FOLLOW_UP_EVIDENCE; drafts are not sent follow-ups.")
    parsed = date.fromisoformat(sent_date)
    if parsed > datetime.now(ZoneInfo("America/Santiago")).date():
        raise ValueError("A sent follow-up cannot have a future date.")
    link = urlparse(evidence)
    if link.scheme != "https" or not link.netloc:
        raise ValueError("FOLLOW_UP_EVIDENCE must be an exact HTTPS evidence URL.")
    return {"lastFollowUpDate": parsed.isoformat(), "lastFollowUpEvidence": evidence}


def validate_follow_up_order(existing: dict, incoming: dict) -> None:
    sent = incoming.get("lastFollowUpDate", "")
    if not sent:
        return
    lower = max(existing.get("lastFollowUpDate") or "", existing.get("appliedDate") or "")
    if sent < lower:
        raise ValueError("Follow-up predates the application or would replace a newer sent follow-up.")
