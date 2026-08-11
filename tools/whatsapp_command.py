#!/usr/bin/env python3
"""Handle short WhatsApp commands against the canonical tracker.

Commands, meant to be triggered by the WhatsApp -> Cloudflare Worker ->
GitHub Actions relay described in docs/WHATSAPP_SETUP.md:

  pipeline
      Compact status digest: stage counts, the next deadline, discovery and
      coverage totals, exact local and cloud monitor times, and tracker link.

  sources
      List every FAILED_SOURCES entry in full, with the manual-fix note.

  sources: <name> | ok|updated
      Remove a FAILED_SOURCES entry (fuzzy company/source match) and log
      it to REVIEWED_SOURCES. 'ok' = no changes needed, 'updated' = fixed.

  update <company> <status> [--date YYYY-MM-DD]
      Find one existing record by company name (fuzzy match) and set its
      status, updating the relevant date field. Does not create new
      records -- new postings still go through update_job_tracker.py with
      a full structured posting file, since that's the only place with
      enough information (job description, requirements, etc).

  raw "<text>"
      Parse a single free-text WhatsApp message and dispatch to one of
      the above. Recognized forms (case-insensitive):
        pipeline
        sources
        sources: <name> | ok|updated
        update: <company> | <status> [| YYYY-MM-DD]

Every command prints a short WhatsApp-ready reply to stdout and exits 0
on success. A disambiguation or not-found message is also printed to
stdout (so it can still be sent back as a reply) but the process exits 1
so the caller knows not to commit a tracker change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from update_job_tracker import (  # noqa: E402
    VALID_STATUSES,
    backup_tracker,
    normalize_key,
    read_tracker,
    updated_html,
    write_atomic,
)
from tracker_context import build_context  # noqa: E402
from discover_postings import (  # noqa: E402
    already_tracked,
    compute_fit,
    identity_keys_from_records,
    last_local_sync_line,
    last_monitor_line,
    read_discarded_postings,
    tracked_identity_keys,
)
from js_literal import read_flat_array, write_flat_array  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state" / "market_history_seen.json"
SYNCED_TRACKED_PATH = REPO_ROOT / "state" / "tracked_identities.json"
MONITOR_SUMMARY_PATH = REPO_ROOT / "state" / "chile_monitor_summary.json"
TRACKER_URL = "https://franciscokirhman.github.io/job-tracker/francisco-job-tracker-2026.html"
MARKET_HISTORY_PATTERN = re.compile(r"const MARKET_HISTORY = (\[.*?\]);", re.S)
VERIFIED_STATUSES = {"open"}
MAX_LISTED = 8
SANTIAGO_TZ = ZoneInfo("America/Santiago")


def all_tracked_keys(tracker: Path) -> set[str]:
    """Union of identity keys from this repo's own SAVED_DATA and
    DISCARDED_POSTINGS, plus whatever the local machine last synced over
    (state/tracked_identities.json) -- covers applications (or discards)
    Francisco made locally that haven't otherwise reached the repo
    (SAVED_DATA itself is deliberately not synced bidirectionally, see
    docs/LOCAL_SYNC_SETUP.md)."""
    keys = tracked_identity_keys(read_tracker(tracker)[1])
    keys |= identity_keys_from_records(read_discarded_postings(tracker.read_text(encoding="utf-8")))
    if SYNCED_TRACKED_PATH.exists():
        keys |= set(json.loads(SYNCED_TRACKED_PATH.read_text(encoding="utf-8")))
    return keys


class CommandError(RuntimeError):
    """Raised for a problem that should still produce a WhatsApp reply."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", required=True, type=Path)
    parser.add_argument("--backup-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("pipeline", help="Print a short status digest")

    update = subparsers.add_parser("update", help="Update an existing record's status")
    update.add_argument("company", help="Company name (fuzzy match)")
    update.add_argument("status", choices=sorted(VALID_STATUSES))
    update.add_argument("--date", help="YYYY-MM-DD, defaults to today")

    raw = subparsers.add_parser("raw", help="Parse one free-text WhatsApp message")
    raw.add_argument("text")

    return parser.parse_args()


def parse_raw_command(text: str) -> tuple[str, tuple[Any, ...]]:
    stripped = text.strip()
    lowered = stripped.casefold()

    if lowered in {"pipeline", "status"}:
        return "pipeline", ()

    if lowered == "sources":
        return "sources_list", ()

    if lowered.startswith("sources"):
        remainder = stripped.split(":", 1)[-1] if ":" in stripped else stripped[len("sources"):]
        parts = [item.strip() for item in remainder.split("|")]
        parts = [item for item in parts if item]
        if len(parts) < 2:
            raise CommandError("Use: sources: <name> | ok  OR  sources: <name> | updated")
        name, action = parts[0], parts[1].casefold()
        if action not in {"ok", "updated"}:
            raise CommandError("Action must be 'ok' (no changes) or 'updated'.")
        return "sources_resolve", (name, action)

    if lowered.startswith("update"):
        remainder = stripped.split(":", 1)[-1] if ":" in stripped else stripped[len("update"):]
        parts = [item.strip() for item in remainder.split("|")]
        parts = [item for item in parts if item]
        if len(parts) < 2:
            raise CommandError(
                "Use: update: <company> | <status> [| YYYY-MM-DD]"
            )
        company, status = parts[0], parts[1].casefold()
        date_str = parts[2] if len(parts) > 2 else None
        if status not in VALID_STATUSES:
            raise CommandError(
                f"Status must be one of {', '.join(sorted(VALID_STATUSES))}, got {parts[1]!r}"
            )
        return "update", (company, status, date_str)

    raise CommandError(
        "Unrecognized command. Send 'pipeline', 'sources', "
        "'sources: <name> | ok|updated', or "
        "'update: <company> | <status> [| YYYY-MM-DD]'."
    )


def read_market_history(tracker: Path) -> list[dict[str, Any]]:
    html = tracker.read_text(encoding="utf-8")
    match = MARKET_HISTORY_PATTERN.search(html)
    if not match:
        return []
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return []


def market_history_key(entry: dict[str, Any]) -> str:
    company = normalize_key(entry.get("company"))
    job_id = normalize_key(entry.get("jobId"))
    if job_id:
        return f"{company}|{job_id}"
    title = normalize_key(entry.get("title"))
    url = normalize_key(entry.get("url"))
    return f"{company}|{title}|{url}"


def new_postings_section(tracker: Path) -> tuple[str, int, int]:
    """Diff MARKET_HISTORY against state/market_history_seen.json.

    First run (no state file) just records the baseline without listing
    anything, so we don't dump the entire existing history as "new" the
    first time this runs.

    Returns (formatted_text, verified_count, unverified_count).
    """
    entries = read_market_history(tracker)
    if not entries:
        return "", 0, 0

    current = {market_history_key(entry): entry for entry in entries}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if STATE_PATH.exists():
        seen = set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        new_keys = [key for key in current if key not in seen]
    else:
        new_keys = []

    STATE_PATH.write_text(
        json.dumps(sorted(current.keys()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not new_keys:
        return "", 0, 0

    new_entries = [current[key] for key in new_keys]
    # "removed" listings aren't actionable new opportunities -- drop them.
    new_entries = [e for e in new_entries if e.get("status") != "removed"]
    # Drop anything already tracked in SAVED_DATA (applied/rejected/interview/
    # etc, including status updates made on the local machine and synced
    # over) -- a MARKET_HISTORY entry being "new" to us doesn't mean the
    # application itself is new.
    tracked_keys = all_tracked_keys(tracker)
    new_entries = [
        e for e in new_entries
        if not already_tracked({"company": e.get("company"), "title": e.get("title"), "jobId": e.get("jobId")}, tracked_keys)
    ]
    verified = [e for e in new_entries if e.get("status") in VERIFIED_STATUSES]
    unverified = [e for e in new_entries if e.get("status") not in VERIFIED_STATUSES]

    verified.sort(key=lambda e: compute_fit(e.get("title", "")), reverse=True)
    unverified.sort(key=lambda e: compute_fit(e.get("title", "")), reverse=True)

    lines = []
    if verified:
        lines.append("")
        lines.append(f"Nuevos puestos ({len(verified)}), por fit:")
        for entry in verified[:MAX_LISTED]:
            fit = compute_fit(entry.get("title", ""))
            lines.append(f"- {entry['company']}: {entry['title']} (fit {fit:.0f}/10) {entry.get('url', '')}")
        if len(verified) > MAX_LISTED:
            lines.append(f"...y {len(verified) - MAX_LISTED} más.")

    if unverified:
        lines.append("")
        lines.append(f"⚠ Sin verificar ({len(unverified)}), revisar manualmente:")
        for entry in unverified[:MAX_LISTED]:
            status = entry.get("status", "")
            lines.append(f"- {entry['company']}: {entry['title']} ({status}) {entry.get('url', '')}")
        if len(unverified) > MAX_LISTED:
            lines.append(f"...y {len(unverified) - MAX_LISTED} más.")

    return "\n".join(lines), len(verified), len(unverified)


def failed_sources_summary(tracker: Path, limit: int = 5) -> tuple[str, int]:
    html = tracker.read_text(encoding="utf-8")
    failed = read_flat_array(html, "FAILED_SOURCES")
    if not failed:
        return "", 0
    lines = ["", f"Fuentes fallidas ({len(failed)}), responde 'sources' para ver todas:"]
    for entry in failed[:limit]:
        lines.append(f"- {entry.get('source')}: {entry.get('status', '')}")
    if len(failed) > limit:
        lines.append(f"...y {len(failed) - limit} más.")
    return "\n".join(lines), len(failed)


def pipeline_digest(tracker: Path) -> str:
    jobs = read_tracker(tracker)[1]
    context = build_context(tracker, jobs)
    stages = context["derivedStageCounts"]
    now_chile = datetime.now(SANTIAGO_TZ)
    lines = [f"Jobs3 update | {now_chile.strftime('%d %b %Y, %H:%M')} Chile"]
    stage_parts = []
    for label, key in (("Applied", "applied"), ("Interview", "interview"), ("Rejected", "rejected")):
        stage_parts.append(f"{label} {stages.get(key, 0)}")
    lines.append(" | ".join(stage_parts))

    today = date.today().isoformat()
    upcoming = sorted(
        (
            job
            for job in jobs
            if job.get("status") == "todo" and job.get("deadline") and job["deadline"] >= today
        ),
        key=lambda job: job["deadline"],
    )[:5]

    _postings_text, verified_count, unverified_count = new_postings_section(tracker)
    discovery_count = verified_count + unverified_count
    lines.append(f"Discovery items to review: {discovery_count}")

    if upcoming:
        next_job = upcoming[0]
        lines.append(f"Next deadline: {next_job['company']} | {next_job['deadline']}")

    sync_line = last_local_sync_line()
    if sync_line:
        lines.append(sync_line)
    monitor_line = last_monitor_line()
    if monitor_line:
        lines.append(monitor_line)
    if MONITOR_SUMMARY_PATH.exists():
        monitor = json.loads(MONITOR_SUMMARY_PATH.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(monitor["latestMonitorChecked"]).astimezone(SANTIAGO_TZ)
        lines.append(f"Official portal monitor: {checked.strftime('%d %b %Y, %H:%M')} Chile")
        lines.append(
            "Retrieval tries: "
            f"{monitor['totalAttempts']} total | "
            f"{monitor['inventoryAttempts']} inventory + {monitor['recoveryAttempts']} recovery"
        )
        lines.append(
            "Official portal audit: "
            f"{monitor['comparable']} of {monitor['configuredSources']} comparable | "
            f"{monitor['unresolved']} incomplete checks"
        )

    return "\n".join(lines)


def sources_list(tracker: Path) -> str:
    html = tracker.read_text(encoding="utf-8")
    failed = read_flat_array(html, "FAILED_SOURCES")
    if not failed:
        return "No hay fuentes fallidas pendientes."
    lines = [f"Fuentes fallidas ({len(failed)}):"]
    for entry in failed:
        lines.append(f"- {entry.get('source')}: {entry.get('status', '')} — {entry.get('manual', '')}")
    lines.append("")
    lines.append("Responde 'sources: <nombre> | ok' (sin cambios) o 'sources: <nombre> | updated' (actualizada).")
    return "\n".join(lines)


def resolve_source(tracker: Path, name: str, action: str) -> str:
    html = tracker.read_text(encoding="utf-8")
    failed = read_flat_array(html, "FAILED_SOURCES")
    key = normalize_key(name)
    if not key:
        return f"No hay fuente fallida que coincida con '{name}'."
    # Same exact-match-first, minimum-length-for-fuzzy-fallback guard as
    # find_company_matches() -- a short query silently matching the wrong
    # source isn't caught by the "multiple matches" check below.
    matches = [e for e in failed if normalize_key(e.get("source")) == key]
    if not matches and len(key) >= MIN_FUZZY_MATCH_LEN:
        matches = [e for e in failed if key in normalize_key(e.get("source"))]

    if not matches:
        return f"No hay fuente fallida que coincida con '{name}'."
    if len(matches) > 1:
        options = ", ".join(e.get("source", "") for e in matches)
        raise CommandError(f"'{name}' coincide con varias fuentes, sé más específico: {options}")

    entry = matches[0]
    remaining = [e for e in failed if e is not entry]
    html = write_flat_array(html, "FAILED_SOURCES", remaining)

    reviewed = read_flat_array(html, "REVIEWED_SOURCES")
    reviewed.insert(
        0,
        {
            "source": entry.get("source", ""),
            "checked": date.today().isoformat(),
            "status": "Actualizada por WhatsApp" if action == "updated" else "Sin cambios, confirmado por WhatsApp",
            "observed": "",
            "evidence": "",
        },
    )
    html = write_flat_array(html, "REVIEWED_SOURCES", reviewed)

    tracker.write_text(html, encoding="utf-8")
    return f"Marcada '{entry.get('source')}' como {'actualizada' if action == 'updated' else 'sin cambios'}."


MIN_FUZZY_MATCH_LEN = 3


def find_company_matches(jobs: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    key = normalize_key(query)
    if not key:
        raise CommandError("Empty company name.")
    # Prefer an exact match over substring matching -- a short/generic
    # query (a typo, an abbreviation) can be a substring of the WRONG
    # company's name and match nothing else, which the "multiple matches"
    # ambiguity check can't catch since it's a single silent wrong match,
    # not an ambiguous one. An exact normalized match is unambiguous by
    # construction. Only fall back to substring matching for queries long
    # enough that an accidental match is unlikely.
    exact = [job for job in jobs if normalize_key(job.get("company", "")) == key]
    if exact:
        return exact
    if len(key) < MIN_FUZZY_MATCH_LEN:
        return []
    matches = [job for job in jobs if key in normalize_key(job.get("company", ""))]
    return matches


def apply_status_update(
    tracker: Path, backup_dir: Path | None, company: str, status: str, date_str: str | None
) -> str:
    html, jobs, match = read_tracker(tracker)
    matches = find_company_matches(jobs, company)

    if not matches:
        return f"No tracked application matches '{company}'."

    distinct_roles = {(job.get("company"), job.get("role")) for job in matches}
    if len(distinct_roles) > 1:
        options = "\n".join(f"- {c} ({r})" for c, r in sorted(distinct_roles))
        raise CommandError(
            f"'{company}' matches more than one application, be more specific:\n{options}"
        )

    record = matches[0]
    when = date_str or date.today().isoformat()
    try:
        date.fromisoformat(when)
    except ValueError as exc:
        raise CommandError(f"Date must be YYYY-MM-DD, got {when!r}") from exc

    previous_status = record.get("status")
    record["status"] = status
    if status == "applied" and not record.get("appliedDate"):
        record["appliedDate"] = when
    elif status == "rejected":
        record["rejectedDate"] = when
    elif status == "interview":
        interviews = record.setdefault("interviews", [])
        # Not idempotent otherwise: a retried/duplicate-delivered WhatsApp
        # webhook, or Francisco resending the same command, would append a
        # second identical round every time. One round per date is a
        # reasonable proxy for "this is the same update happening twice".
        if not any(iv.get("date") == when for iv in interviews):
            interviews.append(
                {
                    "round": len(interviews) + 1,
                    "date": when,
                    "type": "",
                    "notes": "Updated via WhatsApp",
                }
            )
        record["interviewDate"] = when

    if backup_dir is not None:
        backup_tracker(tracker, backup_dir)
    new_html = updated_html(html, match, jobs)
    write_atomic(tracker, new_html)

    return (
        f"Updated {record['company']} ({record['role']}): "
        f"{previous_status} -> {status} ({when})"
    )


def main() -> int:
    args = parse_args()
    try:
        if args.command == "pipeline":
            print(pipeline_digest(args.tracker))
            return 0

        if args.command == "raw":
            kind, params = parse_raw_command(args.text)
            if kind == "pipeline":
                print(pipeline_digest(args.tracker))
                return 0
            if kind == "sources_list":
                print(sources_list(args.tracker))
                return 0
            if kind == "sources_resolve":
                name, action = params
                message = resolve_source(args.tracker, name, action)
                print(message)
                return 0 if message.startswith("Marcada") else 1
            company, status, date_str = params
            message = apply_status_update(
                args.tracker, args.backup_dir, company, status, date_str
            )
            print(message)
            return 0 if message.startswith("Updated") else 1

        message = apply_status_update(
            args.tracker, args.backup_dir, args.company, args.status, args.date
        )
        print(message)
        return 0 if message.startswith("Updated") else 1
    except CommandError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
