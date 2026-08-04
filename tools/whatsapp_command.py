#!/usr/bin/env python3
"""Handle short WhatsApp commands against the canonical tracker.

Two commands, both meant to be triggered by the WhatsApp -> Cloudflare
Worker -> GitHub Actions relay described in docs/WHATSAPP_SETUP.md:

  pipeline
      Print a short status digest (counts per stage, upcoming deadlines).

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
        update: <company> | <status> [| YYYY-MM-DD]

Both commands print a short WhatsApp-ready reply to stdout and exit 0 on
success. A disambiguation or not-found message is also printed to stdout
(so it can still be sent back as a reply) but the process exits 1 so the
caller knows not to commit a tracker change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

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


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state" / "market_history_seen.json"
TRACKER_URL = "https://franciscokirhman.github.io/job-tracker/francisco-job-tracker-2026.html"
MARKET_HISTORY_PATTERN = re.compile(r"const MARKET_HISTORY = (\[.*?\]);", re.S)


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
        "Unrecognized command. Send 'pipeline' or "
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


def new_postings_section(tracker: Path) -> str:
    """Diff MARKET_HISTORY against state/market_history_seen.json.

    First run (no state file) just records the baseline without listing
    anything, so we don't dump the entire existing history as "new" the
    first time this runs.
    """
    entries = read_market_history(tracker)
    if not entries:
        return ""

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
        return ""

    lines = ["", f"Nuevos puestos detectados ({len(new_keys)}):"]
    for key in new_keys[:10]:
        entry = current[key]
        status = entry.get("status", "")
        lines.append(
            f"- {entry.get('company')}: {entry.get('title')} ({status}) {entry.get('url', '')}"
        )
    if len(new_keys) > 10:
        lines.append(f"...y {len(new_keys) - 10} más. Ver el tracker para el resto.")
    return "\n".join(lines)


def pipeline_digest(tracker: Path) -> str:
    jobs = read_tracker(tracker)[1]
    context = build_context(tracker, jobs)
    stages = context["derivedStageCounts"]
    lines = [
        f"Pipeline as of {context['asOfDate']}:",
        ", ".join(f"{stage}: {count}" for stage, count in sorted(stages.items())),
        f"{context['recordCount']} total across {context['companyCount']} companies.",
    ]

    today = date.today().isoformat()
    upcoming = sorted(
        (
            job
            for job in jobs
            if job.get("status") == "todo" and job.get("deadline") and job["deadline"] >= today
        ),
        key=lambda job: job["deadline"],
    )[:5]
    if upcoming:
        lines.append("")
        lines.append("Upcoming deadlines:")
        for job in upcoming:
            lines.append(f"- {job['company']} ({job['role']}): {job['deadline']}")

    postings = new_postings_section(tracker)
    if postings:
        lines.append(postings)

    lines.append("")
    lines.append(f"Tracker: {TRACKER_URL}")

    return "\n".join(lines)


def find_company_matches(jobs: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    key = normalize_key(query)
    if not key:
        raise CommandError("Empty company name.")
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
