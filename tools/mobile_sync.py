#!/usr/bin/env python3
"""Apply a mobile swipe action (discard or save-to-pipeline) to the tracker.

Invoked from .github/workflows/mobile-sync.yml on a `repository_dispatch`
event, which is itself triggered by a small Cloudflare Worker that phone
swipes POST to (see docs/CLOUDFLARE_SYNC_SETUP.md). Mirrors the exact
DISCARDED_POSTINGS shape discardNewJob() writes client-side, and the exact
SAVED_DATA record shape make_todo_record() writes in discover_postings.py,
so a mobile-synced action looks identical to one made through the app
itself or the automated discovery pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_postings import normalize_key  # noqa: E402

DISCARDED_PATTERN = re.compile(
    r"(// DISCARDED_START\s*\nconst DISCARDED_POSTINGS\s*=\s*)(\[[\s\S]*?\])(\s*;\s*\n// DISCARDED_END)"
)
SAVED_DATA_PATTERN = re.compile(
    r"(// TRACKER_DATA_START\s*\nconst SAVED_DATA\s*=\s*)(\[[\s\S]*?\])(\s*;\s*\n// TRACKER_DATA_END)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=["discard", "save"])
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--fit-score", default="0")
    parser.add_argument("--url", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--risk", default="")
    return parser.parse_args()


def identity_key(company: str, role: str) -> str:
    return f"{normalize_key(company)}|{normalize_key(role)}"


def apply_discard(html: str, args: argparse.Namespace) -> tuple[str, str]:
    match = DISCARDED_PATTERN.search(html)
    if not match:
        raise RuntimeError("DISCARDED_POSTINGS block not found in tracker")
    entries = json.loads(match.group(2))
    key = identity_key(args.company, args.role)
    if any(identity_key(e.get("company"), e.get("role")) == key for e in entries):
        return html, f"Already discarded, no change: {args.company} — {args.role}"
    entries.append(
        {
            "company": args.company,
            "role": args.role,
            "jobId": args.job_id,
            "discardedAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    serialized = json.dumps(entries, ensure_ascii=False, indent=2)
    new_html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]
    return new_html, f"Discarded: {args.company} — {args.role}"


def apply_save(html: str, args: argparse.Namespace) -> tuple[str, str]:
    match = SAVED_DATA_PATTERN.search(html)
    if not match:
        raise RuntimeError("SAVED_DATA block not found in tracker")
    jobs = json.loads(match.group(2))
    key = identity_key(args.company, args.role)
    if any(identity_key(j.get("company"), j.get("role")) == key for j in jobs):
        return html, f"Already tracked, no change: {args.company} — {args.role}"

    fit_score = float(args.fit_score or 0)
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    unique_suffix = abs(hash((args.company.casefold(), args.role.casefold()))) % 1000
    record = {
        "id": f"JOB_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{unique_suffix:03d}",
        "priority": "HIGH" if fit_score >= 8 else "MEDIUM" if fit_score >= 5 else "LOW",
        "company": args.company,
        "companyType": "",
        "role": args.role,
        "location": args.location,
        "category": "",
        "jobId": args.job_id,
        "posted": "",
        "deadline": "",
        "status": "todo",
        "appliedDate": "",
        "replyDate": "",
        "rejectedDate": "",
        "interviewDate": "",
        "interviews": [],
        "requirements": [],
        "responsibilities": [],
        "myMatch": [],
        "gaps": [],
        "keywords": [],
        "fitScore": round(fit_score, 1),
        "cvVersion": "",
        "coverLetter": "",
        "folderPath": "",
        "links": args.url,
        "strategicNotes": f"Guardada desde el swipe de Nuevas (mobile sync). Pared o riesgo: {args.risk or '—'}",
        "jobDescription": args.reason,
        "createdAt": today,
    }
    jobs.append(record)
    serialized = json.dumps(jobs, ensure_ascii=False, indent=2)
    new_html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]
    return new_html, f"Saved to pipeline: {args.company} — {args.role} (fit {fit_score:.1f})"


def main() -> int:
    args = parse_args()
    html = args.tracker.read_text(encoding="utf-8")

    try:
        if args.action == "discard":
            new_html, message = apply_discard(html, args)
        else:
            new_html, message = apply_save(html, args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if new_html != html:
        args.tracker.write_text(new_html, encoding="utf-8")
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
