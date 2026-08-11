#!/usr/bin/env python3
"""Build a compact WhatsApp summary from append only Chile portal reports."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SANTIAGO_TZ = ZoneInfo("America/Santiago")
CHECKED_RE = re.compile(r"^Checked:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+America/Santiago", re.M)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--tracker",
        action="append",
        default=[],
        type=Path,
        help="Tracker HTML file that should receive the current embedded monitor snapshot. May be repeated.",
    )
    return parser.parse_args()


def checked_at(text: str) -> datetime:
    match = CHECKED_RE.search(text)
    if not match:
        raise ValueError("Report is missing a valid Checked timestamp")
    return datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M").replace(tzinfo=SANTIAGO_TZ)


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^## ", text[start:], re.M)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end]


def table_rows(block: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in block.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if cells[0].lower() in {"fuente", "estado", "tipo"}:
            continue
        rows.append(cells)
    return rows


def report_kind(text: str) -> str | None:
    first_line = text.splitlines()[0].lower() if text.splitlines() else ""
    if "inventario oficial" in first_line:
        return "inventory"
    if "recovery report" in first_line or "recuperación" in first_line:
        return "recovery"
    return None


def markdown_url(value: str) -> str:
    match = re.search(r"\((https?://[^)]+)\)", value)
    return match.group(1) if match else ""


def status_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def load_reports(reports_dir: Path) -> list[dict[str, object]]:
    reports = []
    for path in reports_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        kind = report_kind(text)
        if not kind:
            continue
        try:
            checked = checked_at(text)
        except ValueError:
            continue
        reports.append({"path": path, "text": text, "kind": kind, "checked": checked})
    return sorted(reports, key=lambda item: item["checked"])


def summary_from_reports(reports_dir: Path) -> dict[str, object]:
    reports = load_reports(reports_dir)
    inventories = [item for item in reports if item["kind"] == "inventory"]
    if not inventories:
        raise ValueError("No valid inventory report found")

    inventory = inventories[-1]
    inventory_path = inventory["path"]
    inventory_text = str(inventory["text"])
    inventory_rows = table_rows(section(inventory_text, "Fuentes oficiales"))
    configured_sources = len(inventory_rows)
    inventory_sources: dict[str, dict[str, str]] = {}
    for row in inventory_rows:
        if len(row) < 4:
            continue
        inventory_sources[row[0]] = {
            "source": row[0],
            "status": row[1].split("—", 1)[0].strip(),
            "lastReliable": row[2],
            "url": markdown_url(row[3]),
            "inventoryEvidence": row[1],
        }
    inventory_attempts = 0
    initial_attempts = 0
    inventory_retries = 0
    for row in inventory_rows:
        evidence = " ".join(row)
        markers = [int(value) for value in re.findall(r"\bP(\d+)\b", evidence, re.I)]
        if markers:
            row_attempts = max(markers)
            inventory_attempts += row_attempts
            initial_attempts += 1 if row_attempts > 0 else 0
            inventory_retries += max(0, row_attempts - 1)
            continue
        lowered = evidence.lower()
        if "primer acceso" in lowered:
            inventory_attempts += 1
            initial_attempts += 1
        if "reintento" in lowered:
            inventory_attempts += 1
            inventory_retries += 1

    compared_marker = f"Compared with: {inventory_path.name}"
    recoveries = [
        item
        for item in reports
        if item["kind"] == "recovery"
        and item["checked"] > inventory["checked"]
        and compared_marker in str(item["text"])
    ]
    recovery = recoveries[-1] if recoveries else None

    recovery_attempts = 0
    retrieved = 0
    confirmed_zero = 0
    unresolved = configured_sources
    recovery_name = None
    recovery_checked = None
    source_rows: list[dict[str, str]] = []

    if recovery:
        recovery_text = str(recovery["text"])
        recovery_rows = table_rows(section(recovery_text, "Estado por fuente oficial"))
        for row in recovery_rows:
            markers = [int(value) for value in re.findall(r"\bP(\d+)\b", " ".join(row), re.I)]
            recovery_attempts += max(markers) if markers else 1
            if len(row) >= 4:
                base = inventory_sources.get(row[0], {})
                source_rows.append(
                    {
                        "source": row[0],
                        "status": row[1],
                        "checked": recovery["checked"].isoformat(),
                        "lastReliable": base.get("lastReliable", "No reliable prior check recorded"),
                        "failure": row[3],
                        "recovery": f"Recovery report {recovery['path'].name}; {row[3]}",
                        "manual": row[3],
                        "filterEvidence": row[2],
                        "url": base.get("url", ""),
                    }
                )

        counts = {}
        for row in table_rows(section(recovery_text, "Resumen de recuperación")):
            if len(row) >= 2 and row[1].isdigit():
                counts[row[0].lower()] = int(row[1])
        retrieved = counts.get("retrieved", 0)
        confirmed_zero = counts.get("confirmed_zero", 0)
        unresolved = counts.get("unavailable / not_comparable", configured_sources - retrieved - confirmed_zero)
        recovery_name = recovery["path"].name
        recovery_checked = recovery["checked"]

    if not source_rows:
        for source in inventory_sources.values():
            source_rows.append(
                {
                    "source": source["source"],
                    "status": source["status"],
                    "checked": inventory["checked"].isoformat(),
                    "lastReliable": source["lastReliable"],
                    "failure": source["inventoryEvidence"],
                    "recovery": "No qualifying recovery report was available for this inventory.",
                    "manual": source["inventoryEvidence"],
                    "filterEvidence": "See inventory report",
                    "url": source["url"],
                }
            )

    comparable = retrieved + confirmed_zero
    latest_checked = recovery_checked or inventory["checked"]
    return {
        "updatedAt": latest_checked.isoformat(),
        "inventoryReport": inventory_path.name,
        "inventoryChecked": inventory["checked"].isoformat(),
        "recoveryReport": recovery_name,
        "recoveryChecked": recovery_checked.isoformat() if recovery_checked else None,
        "latestMonitorChecked": latest_checked.isoformat(),
        "configuredSources": configured_sources,
        "inventoryAttempts": inventory_attempts,
        "recoveryAttempts": recovery_attempts,
        "totalAttempts": inventory_attempts + recovery_attempts,
        "retrieved": retrieved,
        "confirmedZero": confirmed_zero,
        "comparable": comparable,
        "unresolved": unresolved,
        "attemptEvidence": {
            "initialSourceAttempts": initial_attempts,
            "inventoryRetries": inventory_retries,
            "recoverySourceAttempts": recovery_attempts,
        },
        "sources": source_rows,
        "failedSources": [
            source
            for source in source_rows
            if status_key(source["status"]) not in {"retrieved", "confirmed_zero"}
        ],
        "reviewedSources": [
            source
            for source in source_rows
            if status_key(source["status"]) in {"retrieved", "confirmed_zero"}
        ],
    }


MONITOR_BLOCK_RE = re.compile(
    r"// MONITOR_SUMMARY_START\s*\nconst CHILE_MONITOR_SUMMARY\s*=\s*\{[\s\S]*?\};\s*\n// MONITOR_SUMMARY_END"
)


def embed_summary(tracker: Path, summary: dict[str, object]) -> bool:
    html = tracker.read_text(encoding="utf-8")
    serialized = json.dumps(summary, ensure_ascii=False, indent=2)
    block = (
        "// MONITOR_SUMMARY_START\n"
        f"const CHILE_MONITOR_SUMMARY = {serialized};\n"
        "// MONITOR_SUMMARY_END"
    )
    if MONITOR_BLOCK_RE.search(html):
        updated = MONITOR_BLOCK_RE.sub(block, html, count=1)
    else:
        anchor = "const LINKEDIN_DISCOVERY_SOURCE="
        index = html.find(anchor)
        if index < 0:
            raise ValueError(f"Could not find monitor summary insertion point in {tracker}")
        updated = html[:index] + block + "\n" + html[index:]
    if updated == html:
        return False
    tracker.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    args = parse_args()
    summary = summary_from_reports(args.reports_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for tracker in args.tracker:
        embed_summary(tracker, summary)
    print(
        json.dumps(
            {
                "monitorChecked": summary["latestMonitorChecked"],
                "configured": summary["configuredSources"],
                "comparable": summary["comparable"],
                "unresolved": summary["unresolved"],
                "totalAttempts": summary["totalAttempts"],
                "trackersUpdated": len(args.tracker),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
