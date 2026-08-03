#!/usr/bin/env python3
"""Add or update one structured text posting in the Jobs3 HTML tracker."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any


START_MARKER = "// TRACKER_DATA_START"
END_MARKER = "// TRACKER_DATA_END"
VALID_STATUSES = {
    "todo",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
}
LIST_SECTIONS = {
    "REQUIREMENTS",
    "RESPONSIBILITIES",
    "MY MATCH",
    "GAPS",
    "KEYWORDS",
    "LINKS",
    "NOTES",
    "JOB DESCRIPTION",
}


class TrackerError(RuntimeError):
    """Raised when an input cannot be safely added to the tracker."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a structured Jobs3 posting text file and update the embedded "
            "SAVED_DATA array in the HTML tracker."
        )
    )
    parser.add_argument("posting", type=Path, help="Structured posting text file")
    parser.add_argument(
        "--tracker",
        required=True,
        type=Path,
        help="Canonical francisco job tracker HTML file",
    )
    parser.add_argument(
        "--status",
        choices=sorted(VALID_STATUSES),
        help="Override STATUS from the posting",
    )
    parser.add_argument(
        "--applied-date",
        help="Applied date in YYYY-MM-DD format. Defaults to today for applied status.",
    )
    parser.add_argument(
        "--include-assessment",
        action="store_true",
        help=(
            "Import FIT_SCORE, MY MATCH, GAPS, and NOTES. Off by default because "
            "these fields require a factual review against the Master CV."
        ),
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Directory for a timestamped tracker backup before writing",
    )
    parser.add_argument(
        "--store-posting",
        action="store_true",
        help=(
            "Store the structured text file in the canonical application "
            "folder before updating the tracker"
        ),
    )
    parser.add_argument(
        "--application-root",
        type=Path,
        help="Canonical Job Applications directory used with --store-posting",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a backup. Intended for temporary test copies only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and describe the update without changing the tracker",
    )
    parser.add_argument(
        "--print-record",
        action="store_true",
        help="Print the normalized tracker record as JSON",
    )
    return parser.parse_args()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value)


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


def stable_job_id(value: str) -> str:
    """Return a safe deduplication ID, or empty for labels/placeholders."""

    normalized = normalize_key(value)
    if normalized in PLACEHOLDER_JOB_IDS or not re.search(r"\d", normalized):
        return ""
    return normalized


def parse_date(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise TrackerError(f"{field} must use YYYY-MM-DD, received {value!r}") from exc


def parse_template(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise TrackerError(f"Posting file does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise TrackerError(f"Posting must be UTF-8 text: {path}") from exc

    fields: dict[str, str] = {}
    sections: dict[str, list[str]] = {name: [] for name in LIST_SECTIONS}
    current_section: str | None = None
    field_pattern = re.compile(r"^([A-Z_0-9]+)\s*:\s*(.*)$")
    section_pattern = re.compile(r"^-{2,}\s*([A-Z][A-Z &/]+?)\s*-{2,}$")

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        section_match = section_pattern.match(stripped)
        if section_match:
            name = section_match.group(1).strip()
            current_section = name if name in sections else None
            continue

        if current_section is not None:
            if not stripped:
                continue
            if current_section == "KEYWORDS":
                sections[current_section].extend(
                    item.strip() for item in stripped.split(",") if item.strip()
                )
            else:
                sections[current_section].append(stripped)
            continue

        field_match = field_pattern.match(stripped)
        if field_match:
            fields[field_match.group(1)] = field_match.group(2).strip()

    if not fields.get("COMPANY"):
        raise TrackerError("Posting is missing COMPANY")
    if not fields.get("ROLE"):
        raise TrackerError("Posting is missing ROLE")
    return fields, sections


def source_folder_path(posting: Path, supplied: str) -> str:
    posting_parent = posting.resolve().parent
    application_root = Path(
        "/Users/franciscokirhman/Documents/Career/Job Applications"
    )
    try:
        posting_parent.relative_to(application_root)
        return str(posting_parent)
    except ValueError:
        pass

    if supplied:
        candidate = Path(os.path.expanduser(supplied))
        if candidate.exists():
            return str(candidate.resolve())
    return ""


def filename_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_")
    return slug or "Unknown"


def planned_posting_path(
    posting: Path,
    fields: dict[str, str],
    application_root: Path,
) -> Path:
    resolved_posting = posting.resolve()
    resolved_root = application_root.expanduser().resolve()
    try:
        resolved_posting.relative_to(resolved_root)
        return resolved_posting
    except ValueError:
        pass

    supplied_folder = fields.get("FOLDER_PATH", "")
    if supplied_folder:
        supplied_path = Path(os.path.expanduser(supplied_folder))
        if supplied_path.exists():
            resolved_supplied = supplied_path.resolve()
            try:
                resolved_supplied.relative_to(resolved_root)
                target_folder = resolved_supplied
            except ValueError:
                target_folder = resolved_root / (
                    filename_slug(fields["COMPANY"])
                    + "_"
                    + filename_slug(fields["ROLE"])
                )
        else:
            target_folder = resolved_root / (
                filename_slug(fields["COMPANY"])
                + "_"
                + filename_slug(fields["ROLE"])
            )
    else:
        target_folder = resolved_root / (
            filename_slug(fields["COMPANY"])
            + "_"
            + filename_slug(fields["ROLE"])
        )

    name_parts = [
        "Job_Posting",
        filename_slug(fields["COMPANY"]),
        filename_slug(fields["ROLE"]),
    ]
    if fields.get("JOB_ID"):
        name_parts.append(filename_slug(fields["JOB_ID"]))
    return target_folder / ("_".join(name_parts) + ".txt")


def store_posting(source: Path, destination: Path) -> str:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return "already stored"
    if destination.exists():
        if source.read_bytes() == destination.read_bytes():
            return "already stored"
        raise TrackerError(
            "A different posting already exists at the planned destination: "
            f"{destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return "created"


def make_record(
    posting: Path,
    fields: dict[str, str],
    sections: dict[str, list[str]],
    status_override: str | None,
    applied_date_override: str | None,
    include_assessment: bool,
    folder_path_override: str | None = None,
) -> dict[str, Any]:
    status_value = (status_override or fields.get("STATUS") or "todo").casefold()
    if status_value not in VALID_STATUSES:
        raise TrackerError(
            f"STATUS must be one of {', '.join(sorted(VALID_STATUSES))}"
        )

    supplied_applied_date = applied_date_override or fields.get("APPLIED_DATE", "")
    applied_date = parse_date(supplied_applied_date, "APPLIED_DATE")
    if applied_date_override and status_override is None:
        status_value = "applied"
    if status_value == "applied" and not applied_date:
        applied_date = date.today().isoformat()

    posted = parse_date(fields.get("POSTED", ""), "POSTED")
    deadline = parse_date(fields.get("DEADLINE", ""), "DEADLINE")
    reply_date = parse_date(fields.get("REPLY_DATE", ""), "REPLY_DATE")
    rejected_date = parse_date(fields.get("REJECTED_DATE", ""), "REJECTED_DATE")

    priority = (fields.get("PRIORITY") or "MEDIUM").upper()
    if priority not in {"HIGH", "MEDIUM", "LOW"}:
        raise TrackerError("PRIORITY must be HIGH, MEDIUM, or LOW")

    fit_score = 0.0
    if include_assessment and fields.get("FIT_SCORE"):
        try:
            fit_score = float(fields["FIT_SCORE"])
        except ValueError as exc:
            raise TrackerError("FIT_SCORE must be numeric") from exc
        if not 0 <= fit_score <= 10:
            raise TrackerError("FIT_SCORE must be between 0 and 10")

    interviews: list[dict[str, Any]] = []
    for number in range(1, 10):
        value = fields.get(f"INTERVIEW_{number}", "")
        if not value:
            continue
        parts = [item.strip() for item in value.split("|", 2)]
        while len(parts) < 3:
            parts.append("")
        interviews.append(
            {
                "round": number,
                "date": parse_date(parts[0], f"INTERVIEW_{number}"),
                "type": parts[1],
                "notes": parts[2],
            }
        )

    company = normalize_space(fields["COMPANY"])
    role = normalize_space(fields["ROLE"])
    created_at = date.today().isoformat()
    unique_suffix = abs(hash((company.casefold(), role.casefold()))) % 1000

    return {
        "id": f"JOB_{int(time.time() * 1000)}_{unique_suffix:03d}",
        "createdAt": created_at,
        "company": company,
        "companyType": normalize_space(fields.get("COMPANY_TYPE", "")),
        "role": role,
        "location": normalize_space(
            fields.get("LOCATION", "") or "Santiago, Chile"
        ),
        "category": normalize_space(fields.get("CATEGORY", "")),
        "jobId": normalize_space(fields.get("JOB_ID", "")),
        "posted": posted,
        "deadline": deadline,
        "priority": priority,
        "status": status_value,
        "appliedDate": applied_date,
        "replyDate": reply_date,
        "rejectedDate": rejected_date,
        "interviewDate": interviews[0]["date"] if interviews else "",
        "interviews": interviews,
        "requirements": sections["REQUIREMENTS"],
        "responsibilities": sections["RESPONSIBILITIES"],
        "myMatch": sections["MY MATCH"] if include_assessment else [],
        "gaps": sections["GAPS"] if include_assessment else [],
        "keywords": sections["KEYWORDS"],
        "fitScore": fit_score,
        "cvVersion": fields.get("CV_VERSION", ""),
        "coverLetter": fields.get("COVER_LETTER", ""),
        "folderPath": folder_path_override
        or source_folder_path(posting, fields.get("FOLDER_PATH", "")),
        "links": "\n".join(sections["LINKS"]),
        "strategicNotes": (
            "\n".join(sections["NOTES"]) if include_assessment else ""
        ),
        "jobDescription": "\n".join(sections["JOB DESCRIPTION"]),
    }


def read_tracker(path: Path) -> tuple[str, list[dict[str, Any]], re.Match[str]]:
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TrackerError(f"Tracker does not exist: {path}") from exc

    pattern = re.compile(
        rf"({re.escape(START_MARKER)}\s*\nconst SAVED_DATA\s*=\s*)"
        rf"(\[[\s\S]*?\])"
        rf"(\s*;\s*\n{re.escape(END_MARKER)})"
    )
    match = pattern.search(html)
    if not match:
        raise TrackerError("Tracker data markers or SAVED_DATA array were not found")
    try:
        jobs = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise TrackerError(f"Tracker SAVED_DATA is not valid JSON: {exc}") from exc
    if not isinstance(jobs, list):
        raise TrackerError("Tracker SAVED_DATA must be a JSON array")
    return html, jobs, match


def find_existing(
    jobs: list[dict[str, Any]], record: dict[str, Any]
) -> tuple[int | None, str]:
    new_job_id = stable_job_id(str(record.get("jobId", "")))
    if new_job_id:
        for index, job in enumerate(jobs):
            if stable_job_id(str(job.get("jobId", ""))) == new_job_id:
                return index, "job ID"

    company_key = normalize_key(record["company"])
    role_key = normalize_key(record["role"])
    for index, job in enumerate(jobs):
        if (
            normalize_key(str(job.get("company", ""))) == company_key
            and normalize_key(str(job.get("role", ""))) == role_key
        ):
            return index, "company and role"
    return None, ""


def merge_record(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(existing)
    preserve_if_empty = {
        "requirements",
        "responsibilities",
        "myMatch",
        "gaps",
        "keywords",
        "jobDescription",
        "links",
        "strategicNotes",
        "cvVersion",
        "coverLetter",
        "folderPath",
        "interviews",
    }
    for key, value in incoming.items():
        if key in {"id", "createdAt"}:
            continue
        if key in preserve_if_empty and not value:
            continue
        if value == "" and existing.get(key):
            continue
        if key == "fitScore" and value == 0 and existing.get(key):
            continue
        merged[key] = value
    merged["id"] = existing.get("id") or incoming["id"]
    merged["createdAt"] = existing.get("createdAt") or incoming["createdAt"]
    return merged


def updated_html(
    html: str, match: re.Match[str], jobs: list[dict[str, Any]]
) -> str:
    serialized = json.dumps(jobs, ensure_ascii=False, indent=2)
    replacement = f"{match.group(1)}{serialized}{match.group(3)}"
    return html[: match.start()] + replacement + html[match.end() :]


def write_atomic(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def backup_tracker(tracker: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    destination = backup_dir / f"{tracker.stem}_before_{stamp}{tracker.suffix}"
    shutil.copy2(tracker, destination)
    return destination


def main() -> int:
    args = parse_args()
    try:
        fields, sections = parse_template(args.posting)
        planned_path: Path | None = None
        if args.store_posting:
            if args.application_root is None:
                raise TrackerError(
                    "--application-root is required with --store-posting"
                )
            planned_path = planned_posting_path(
                args.posting, fields, args.application_root
            )
        record = make_record(
            args.posting,
            fields,
            sections,
            args.status,
            args.applied_date,
            args.include_assessment,
            str(planned_path.parent) if planned_path else None,
        )
        html, jobs, match = read_tracker(args.tracker)
        existing_index, duplicate_key = find_existing(jobs, record)

        if existing_index is None:
            jobs.append(record)
            action = "add"
        else:
            existing = jobs[existing_index]
            if (
                args.status is None
                and record["status"] == "todo"
                and existing.get("status") not in {"", None, "todo"}
            ):
                record["status"] = existing["status"]
            if (
                not args.applied_date
                and not fields.get("APPLIED_DATE")
                and existing.get("appliedDate")
            ):
                record["appliedDate"] = existing["appliedDate"]
            jobs[existing_index] = merge_record(jobs[existing_index], record)
            record = jobs[existing_index]
            action = "update"

        if args.print_record:
            print(json.dumps(record, ensure_ascii=False, indent=2))

        print(f"Action: {action}")
        if duplicate_key:
            print(f"Matched existing record by: {duplicate_key}")
        print(f"Company: {record['company']}")
        print(f"Role: {record['role']}")
        print(f"Job ID: {record['jobId'] or '(none)'}")
        print(f"Status: {record['status']}")
        print(f"Applied date: {record['appliedDate'] or '(none)'}")
        if planned_path:
            print(f"Posting file: {planned_path}")
        print(f"Tracker records after update: {len(jobs)}")

        if args.dry_run:
            print("Dry run: tracker was not changed")
            return 0

        if planned_path:
            posting_action = store_posting(args.posting, planned_path)
            print(f"Posting storage: {posting_action}")

        if not args.no_backup:
            if args.backup_dir is None:
                raise TrackerError(
                    "A backup directory is required unless --no-backup is used"
                )
            backup_path = backup_tracker(args.tracker, args.backup_dir)
            print(f"Backup: {backup_path}")

        new_html = updated_html(html, match, jobs)
        write_atomic(args.tracker, new_html)

        _, verified_jobs, _ = read_tracker(args.tracker)
        if len(verified_jobs) != len(jobs):
            raise TrackerError("Post write verification found a record count mismatch")
        verified_index, _ = find_existing(verified_jobs, record)
        if verified_index is None:
            raise TrackerError("Post write verification could not find the saved record")

        verified_record = verified_jobs[verified_index]
        verification_fields = {
            "company": record["company"],
            "role": record["role"],
            "status": record["status"],
            "appliedDate": record["appliedDate"],
            "folderPath": record["folderPath"],
        }
        if record["jobId"]:
            verification_fields["jobId"] = record["jobId"]
        for field, expected in verification_fields.items():
            actual = verified_record.get(field, "")
            if actual != expected:
                raise TrackerError(
                    "Post write verification mismatch for "
                    f"{field}: expected {expected!r}, received {actual!r}"
                )

        if planned_path:
            if not planned_path.is_file():
                raise TrackerError(
                    f"Post write verification could not find the stored posting: {planned_path}"
                )
            if planned_path.resolve() != args.posting.resolve():
                if planned_path.read_bytes() != args.posting.resolve().read_bytes():
                    raise TrackerError(
                        "Post write verification found different source and stored posting content"
                    )

        print(f"Updated tracker: {args.tracker}")
        print("Verified saved record: company, role, status, applied date, and folder")
        if record["jobId"]:
            print("Verified saved job ID")
        if planned_path:
            print(f"Verified stored posting: {planned_path}")
        return 0
    except TrackerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
