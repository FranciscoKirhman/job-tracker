#!/usr/bin/env python3
"""Read the canonical Jobs3 tracker and print live AI context without writing it."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any


START_MARKER = "// TRACKER_DATA_START"
END_MARKER = "// TRACKER_DATA_END"
PLACEHOLDER_JOB_IDS = {
    "",
    "linkedin",
    "na",
    "none",
    "notprovided",
    "pending",
    "tbd",
    "unknown",
}


class ContextError(RuntimeError):
    """Raised when the tracker cannot be read safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print current tracker context without modifying the tracker."
    )
    parser.add_argument("tracker", type=Path, help="Canonical tracker HTML")
    parser.add_argument(
        "--format",
        choices=("json", "researcher", "applier"),
        default="json",
        help="Output format. JSON is the machine readable default.",
    )
    parser.add_argument(
        "--assert-clean",
        action="store_true",
        help="Exit with status 3 when duplicate application identities exist.",
    )
    return parser.parse_args()


def normalize_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value)


def stable_job_id(value: Any) -> str:
    normalized = normalize_key(value)
    if normalized in PLACEHOLDER_JOB_IDS or not re.search(r"\d", normalized):
        return ""
    return normalized


def duplicate_key(record: dict[str, Any]) -> str:
    job_id = stable_job_id(record.get("jobId"))
    if job_id:
        return f"job_id:{job_id}"
    return (
        "company_role:"
        + normalize_key(record.get("company"))
        + "|"
        + normalize_key(record.get("role"))
    )


def same_identity(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_job_id = stable_job_id(first.get("jobId"))
    second_job_id = stable_job_id(second.get("jobId"))
    if first_job_id and second_job_id:
        return first_job_id == second_job_id
    return (
        normalize_key(first.get("company"))
        == normalize_key(second.get("company"))
        and normalize_key(first.get("role")) == normalize_key(second.get("role"))
    )


def read_tracker(path: Path) -> list[dict[str, Any]]:
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContextError(f"Tracker does not exist: {path}") from exc

    pattern = re.compile(
        rf"{re.escape(START_MARKER)}\s*\n"
        rf"const SAVED_DATA\s*=\s*(\[[\s\S]*?\])"
        rf"\s*;\s*\n{re.escape(END_MARKER)}"
    )
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise ContextError(
            "Expected exactly one embedded SAVED_DATA block, "
            f"found {len(matches)}"
        )
    try:
        jobs = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc:
        raise ContextError(f"Embedded SAVED_DATA is not valid JSON: {exc}") from exc
    if not isinstance(jobs, list):
        raise ContextError("Embedded SAVED_DATA must be a JSON array")
    return jobs


def interview_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    interviews = record.get("interviews")
    if not isinstance(interviews, list):
        return []
    return [
        item
        for item in interviews
        if isinstance(item, dict)
        and (item.get("date") or item.get("notes") or item.get("type"))
    ]


def reached_interview(record: dict[str, Any]) -> bool:
    return bool(interview_entries(record)) or record.get("status") in {
        "interview",
        "offer",
    }


def derived_stage(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "unknown")
    if status in {"offer", "rejected", "withdrawn"}:
        return status
    if reached_interview(record):
        return "interview"
    return status


def days_between(start: Any, end: Any) -> int | None:
    try:
        return (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except (TypeError, ValueError):
        return None


def latest_activity(record: dict[str, Any], today: str) -> str:
    status = str(record.get("status") or "")
    rejected_date = str(record.get("rejectedDate") or "")
    if status == "rejected":
        return (
            f"rejected {rejected_date}"
            if rejected_date
            else "rejected, date not recorded"
        )

    interviews = sorted(
        interview_entries(record), key=lambda item: str(item.get("date") or "")
    )
    if interviews:
        latest = interviews[-1]
        label = latest.get("round") or len(interviews)
        event_date = latest.get("date") or "date not recorded"
        return f"interview round {label} {event_date}"

    reply_date = str(record.get("replyDate") or "")
    if reply_date:
        return f"first reply {reply_date}"

    applied_date = str(record.get("appliedDate") or "")
    waiting = days_between(applied_date, today)
    if waiting is not None:
        return f"no recorded reply for {waiting} days"
    return ""


def duplicate_groups(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parents = list(range(len(jobs)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = root(first)
        second_root = root(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first in range(len(jobs)):
        for second in range(first + 1, len(jobs)):
            if same_identity(jobs[first], jobs[second]):
                union(first, second)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, job in enumerate(jobs):
        grouped[root(index)].append(job)
    return [
        {
            "key": duplicate_key(records[0]),
            "records": [
                {
                    "id": item.get("id"),
                    "company": item.get("company"),
                    "role": item.get("role"),
                    "jobId": item.get("jobId"),
                }
                for item in records
            ],
        }
        for _, records in sorted(grouped.items())
        if len(records) > 1
    ]


def factual_record(record: dict[str, Any], today: str) -> dict[str, Any]:
    interviews = interview_entries(record)
    return {
        "id": record.get("id"),
        "company": record.get("company"),
        "role": record.get("role"),
        "jobId": record.get("jobId"),
        "location": record.get("location"),
        "category": record.get("category"),
        "status": record.get("status"),
        "derivedStage": derived_stage(record),
        "appliedDate": record.get("appliedDate"),
        "replyDate": record.get("replyDate"),
        "rejectedDate": record.get("rejectedDate"),
        "interviews": interviews,
        "reachedInterview": reached_interview(record),
        "latestActivity": latest_activity(record, today),
        "cvVersion": record.get("cvVersion"),
        "folderPath": record.get("folderPath"),
        "dedupKey": duplicate_key(record),
    }


def build_context(path: Path, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    today = date.today().isoformat()
    raw_status = Counter(str(job.get("status") or "unknown") for job in jobs)
    stages = Counter(derived_stage(job) for job in jobs)
    records = [factual_record(job, today) for job in jobs]
    interview_reached = sum(1 for job in jobs if reached_interview(job))
    interview_rounds = sum(len(interview_entries(job)) for job in jobs)
    warnings: list[dict[str, Any]] = []

    for job in jobs:
        interviews = interview_entries(job)
        if interviews and job.get("status") == "applied":
            warnings.append(
                {
                    "type": "status_interview_mismatch",
                    "company": job.get("company"),
                    "role": job.get("role"),
                    "message": "Interview history exists while raw status is Applied.",
                }
            )
        if job.get("status") == "rejected" and not job.get("rejectedDate"):
            warnings.append(
                {
                    "type": "missing_rejection_date",
                    "company": job.get("company"),
                    "role": job.get("role"),
                    "message": "Rejected status has no recorded rejection date.",
                }
            )

    return {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOfDate": today,
        "source": str(path.resolve()),
        "sourceModifiedAt": datetime.fromtimestamp(path.stat().st_mtime)
        .astimezone()
        .isoformat(timespec="seconds"),
        "recordCount": len(jobs),
        "companyCount": len({str(job.get("company") or "") for job in jobs}),
        "statusCounts": dict(sorted(raw_status.items())),
        "derivedStageCounts": dict(sorted(stages.items())),
        "applicationsEverInterviewed": interview_reached,
        "totalInterviewRounds": interview_rounds,
        "duplicates": duplicate_groups(jobs),
        "integrityWarnings": warnings,
        "records": records,
    }


def researcher_text(context: dict[str, Any]) -> str:
    lines = [
        f"ALREADY TRACKED JOB APPLICATIONS (as of {context['asOfDate']})",
        (
            f"Total: {context['recordCount']} positions across "
            f"{context['companyCount']} companies."
        ),
        (
            "Exclude every position below from new job research. Treat a stable "
            "Job ID as authoritative; otherwise match normalized company plus role. "
            "A repost or refreshed posting is still a duplicate."
        ),
        "",
        "| Company | Role | Job ID | Status | Applied | Latest activity |",
        "|---|---|---|---|---|---|",
    ]
    for record in sorted(
        context["records"], key=lambda item: (item["company"] or "", item["role"] or "")
    ):
        cells = [
            record["company"],
            record["role"],
            record["jobId"],
            record["status"],
            record["appliedDate"],
            record["latestActivity"],
        ]
        lines.append(
            "| " + " | ".join(str(item or "").replace("|", "/") for item in cells) + " |"
        )
    return "\n".join(lines)


def applier_text(context: dict[str, Any]) -> str:
    lines = [
        f"LIVE JOB APPLICATION CONTEXT (as of {context['asOfDate']})",
        (
            f"{context['recordCount']} applications, "
            f"{context['applicationsEverInterviewed']} reached interview, "
            f"{context['totalInterviewRounds']} interview rounds logged."
        ),
        "Raw status is preserved. Derived stage only reconciles analytics.",
        "",
    ]
    for record in sorted(
        context["records"],
        key=lambda item: (item["derivedStage"] or "", item["appliedDate"] or ""),
    ):
        line = (
            f"{record['company']} | {record['role']} | "
            f"status: {record['status']} | stage: {record['derivedStage']}"
        )
        if record["jobId"]:
            line += f" | ID {record['jobId']}"
        if record["appliedDate"]:
            line += f" | applied {record['appliedDate']}"
        if record["latestActivity"]:
            line += f" | {record['latestActivity']}"
        lines.append(line)
        for interview in record["interviews"]:
            lines.append(
                "  interview "
                + str(interview.get("round") or "")
                + ": "
                + str(interview.get("date") or "date not recorded")
                + (f" | {interview.get('type')}" if interview.get("type") else "")
                + (f" | {interview.get('notes')}" if interview.get("notes") else "")
            )
    if context["integrityWarnings"]:
        lines.extend(("", "INTEGRITY WARNINGS"))
        for warning in context["integrityWarnings"]:
            lines.append(
                f"{warning['company']} | {warning['role']} | {warning['message']}"
            )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        jobs = read_tracker(args.tracker)
        context = build_context(args.tracker, jobs)
        if args.format == "researcher":
            print(researcher_text(context))
        elif args.format == "applier":
            print(applier_text(context))
        else:
            json.dump(context, sys.stdout, ensure_ascii=False, indent=2)
            print()
        if args.assert_clean and context["duplicates"]:
            return 3
        return 0
    except ContextError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
