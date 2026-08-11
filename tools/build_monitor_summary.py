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

    if recovery:
        recovery_text = str(recovery["text"])
        recovery_rows = table_rows(section(recovery_text, "Estado por fuente oficial"))
        for row in recovery_rows:
            markers = [int(value) for value in re.findall(r"\bP(\d+)\b", " ".join(row), re.I)]
            recovery_attempts += max(markers) if markers else 1

        counts = {}
        for row in table_rows(section(recovery_text, "Resumen de recuperación")):
            if len(row) >= 2 and row[1].isdigit():
                counts[row[0].lower()] = int(row[1])
        retrieved = counts.get("retrieved", 0)
        confirmed_zero = counts.get("confirmed_zero", 0)
        unresolved = counts.get("unavailable / not_comparable", configured_sources - retrieved - confirmed_zero)
        recovery_name = recovery["path"].name
        recovery_checked = recovery["checked"]

    comparable = retrieved + confirmed_zero
    latest_checked = recovery_checked or inventory["checked"]
    return {
        "updatedAt": datetime.now(SANTIAGO_TZ).isoformat(),
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
    }


def main() -> int:
    args = parse_args()
    summary = summary_from_reports(args.reports_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
