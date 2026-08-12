#!/usr/bin/env python3
"""Publish canonical local application records into the GitHub tracker copy.

This is intentionally one way. The local tracker is the Jobs3 source of truth
for application records and statuses. Discovery metadata is synchronized by a
separate union merge, but stale GitHub SAVED_DATA must never overwrite local
records.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tracker_context import duplicate_groups


SAVED_DATA_PATTERN = re.compile(
    r"(// TRACKER_DATA_START\s*\nconst SAVED_DATA\s*=\s*)"
    r"(\[[\s\S]*?\])"
    r"(\s*;\s*\n// TRACKER_DATA_END)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    return parser.parse_args()


def saved_data_block(path: Path) -> tuple[str, re.Match[str], list[dict]]:
    html = path.read_text(encoding="utf-8")
    matches = list(SAVED_DATA_PATTERN.finditer(html))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one SAVED_DATA block in {path}, found {len(matches)}"
        )
    match = matches[0]
    try:
        records = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid SAVED_DATA JSON in {path}: {exc}") from exc
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RuntimeError(f"SAVED_DATA in {path} must be an array of objects")
    return html, match, records


def assert_unique(records: list[dict], path: Path) -> None:
    duplicates = duplicate_groups(records)
    if duplicates:
        raise RuntimeError(
            f"Refusing to publish duplicate application identities from {path}: {duplicates[:5]}"
        )


def publish(canonical: Path, repo: Path) -> tuple[bool, int, int]:
    _canonical_html, canonical_match, canonical_records = saved_data_block(canonical)
    repo_html, repo_match, repo_records = saved_data_block(repo)
    assert_unique(canonical_records, canonical)

    if canonical_records == repo_records:
        return False, len(canonical_records), len(repo_records)

    serialized = json.dumps(canonical_records, ensure_ascii=False, indent=2)
    updated = (
        repo_html[: repo_match.start()]
        + repo_match.group(1)
        + serialized
        + repo_match.group(3)
        + repo_html[repo_match.end() :]
    )
    repo.write_text(updated, encoding="utf-8")
    return True, len(canonical_records), len(repo_records)


def main() -> int:
    args = parse_args()
    changed, canonical_count, previous_repo_count = publish(args.canonical, args.repo)
    print(
        f"Canonical application snapshot: {canonical_count} records "
        f"(repo previously {previous_repo_count})."
    )
    print(f"CANONICAL_CHANGED={'1' if changed else '0'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
