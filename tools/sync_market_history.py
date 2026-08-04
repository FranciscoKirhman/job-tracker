#!/usr/bin/env python3
"""Merge MARKET_HISTORY (discovered job postings) between two tracker files.

Scoped to MARKET_HISTORY only, deliberately -- SAVED_DATA (application
records/status) is NOT touched here. WhatsApp two-way commands mutate
SAVED_DATA directly in the repo; a blind local<->repo merge of SAVED_DATA
could clobber those changes. MARKET_HISTORY is pure discovery metadata
(company, title, url, timestamps) with no such conflict, so it's safe to
union in both directions.

Usage:
    python3 sync_market_history.py --local <path> --repo <path>

Writes the merged array back into BOTH files (only if it actually changed
for that file) and prints a one-line summary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


MARKET_HISTORY_PATTERN = re.compile(r"(const MARKET_HISTORY = )(\[.*?\])(;)", re.S)
DISCARDED_PATTERN = re.compile(
    r"(// DISCARDED_START\s*\nconst DISCARDED_POSTINGS\s*=\s*)(\[[\s\S]*?\])(\s*;\s*\n// DISCARDED_END)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument(
        "--tracked-state",
        type=Path,
        help="Where to write the local machine's tracked-application identity "
        "keys, read-only input for the cloud side's already-tracked checks. "
        "Defaults to state/tracked_identities.json next to --repo.",
    )
    return parser.parse_args()


def normalize_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_value)


def entry_key(entry: dict[str, Any]) -> str:
    company = normalize_key(entry.get("company"))
    job_id = normalize_key(entry.get("jobId"))
    if job_id:
        return f"{company}|{job_id}"
    return f"{company}|{normalize_key(entry.get('title'))}|{normalize_key(entry.get('url'))}"


def read_market_history(path: Path) -> tuple[str, list[dict[str, Any]], re.Match[str] | None]:
    html = path.read_text(encoding="utf-8")
    match = MARKET_HISTORY_PATTERN.search(html)
    if not match:
        return html, [], None
    try:
        entries = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        # A parse failure here used to silently become "this side has zero
        # entries", which then merges as if every entry that only exists on
        # the corrupted side were new -- and, worse, write_if_changed()
        # would happily overwrite the corrupted file with a "recovered"
        # version built while blind to whatever was actually there. Fail
        # loudly instead: a corrupt MARKET_HISTORY block needs a human to
        # look at it, not an automated merge silently treating it as empty.
        raise RuntimeError(f"MARKET_HISTORY in {path} is not valid JSON: {exc}") from exc
    return html, entries, match


def merge(local_entries: list[dict], repo_entries: list[dict]) -> list[dict]:
    by_key: dict[str, dict[str, Any]] = {}
    for entry in repo_entries + local_entries:
        key = entry_key(entry)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = entry
            continue
        # Prefer whichever has the more recent lastConfirmed timestamp.
        if str(entry.get("lastConfirmed", "")) > str(existing.get("lastConfirmed", "")):
            by_key[key] = entry
    return [by_key[key] for key in by_key]


def discarded_key(entry: dict[str, Any]) -> str:
    return f"{normalize_key(entry.get('company'))}|{normalize_key(entry.get('role'))}"


def read_discarded(path: Path) -> tuple[str, list[dict[str, Any]], re.Match[str] | None]:
    html = path.read_text(encoding="utf-8")
    match = DISCARDED_PATTERN.search(html)
    if not match:
        return html, [], None
    try:
        entries = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DISCARDED_POSTINGS in {path} is not valid JSON: {exc}") from exc
    return html, entries, match


def merge_discarded(local_entries: list[dict], repo_entries: list[dict]) -> list[dict]:
    by_key: dict[str, dict[str, Any]] = {}
    for entry in repo_entries + local_entries:
        key = discarded_key(entry)
        existing = by_key.get(key)
        if existing is None or str(entry.get("discardedAt", "")) > str(existing.get("discardedAt", "")):
            by_key[key] = entry
    return [by_key[key] for key in by_key]


def write_discarded_if_changed(
    path: Path, html: str, match: re.Match[str], merged: list[dict], existing: list[dict]
) -> bool:
    # Comparing key SETS instead of content meant a discardedAt refresh (or
    # any other field-level update merge() picked from the other side)
    # never actually got written -- same keys before and after, so this
    # returned False and the file kept its stale copy forever. Compare the
    # full keyed content instead (dict equality, so key order doesn't
    # matter but any value difference does).
    if {discarded_key(e): e for e in existing} == {discarded_key(e): e for e in merged}:
        return False
    serialized = json.dumps(merged, ensure_ascii=False, indent=2)
    new_html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]
    path.write_text(new_html, encoding="utf-8")
    return True


def write_if_changed(
    path: Path, html: str, match: re.Match[str], merged: list[dict], existing: list[dict]
) -> bool:
    # Same fix as write_discarded_if_changed(): compare keyed CONTENT, not
    # just the set of identity keys, so a merge() pick of the fresher
    # lastConfirmed/status/etc. for an already-shared entry actually gets
    # persisted instead of being silently discarded as "nothing changed".
    if {entry_key(entry): entry for entry in existing} == {entry_key(entry): entry for entry in merged}:
        return False
    serialized = json.dumps(merged, ensure_ascii=False, indent=2)
    new_html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]
    path.write_text(new_html, encoding="utf-8")
    return True


SAVED_DATA_PATTERN = re.compile(
    r"// TRACKER_DATA_START\s*\nconst SAVED_DATA\s*=\s*(\[[\s\S]*?\])\s*;\s*\n// TRACKER_DATA_END"
)


def read_saved_data(html: str) -> list[dict[str, Any]]:
    match = SAVED_DATA_PATTERN.search(html)
    if not match:
        return []
    return json.loads(match.group(1))


def tracked_identity_keys(saved_data: list[dict[str, Any]]) -> set[str]:
    keys = set()
    for record in saved_data:
        company = normalize_key(record.get("company"))
        role = normalize_key(record.get("role"))
        if company and role:
            keys.add(f"{company}|{role}")
        job_id = normalize_key(record.get("jobId"))
        if job_id and re.search(r"\d", job_id):
            keys.add(f"jobid|{job_id}")
    return keys


def sync_tracked_identities(local_html: str, state_path: Path) -> tuple[bool, int]:
    """One-way, read-only local -> repo sync of application identity keys
    (never job data itself, just enough to recognize 'already applied to
    this'). Additive only -- never removes a key -- so the cloud side's
    already-tracked checks can only get MORE cautious over time, never less.
    """
    new_keys = tracked_identity_keys(read_saved_data(local_html))
    existing: set[str] = set()
    if state_path.exists():
        existing = set(json.loads(state_path.read_text(encoding="utf-8")))
    merged = existing | new_keys
    if merged == existing:
        return False, 0
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(merged), ensure_ascii=False, indent=2), encoding="utf-8")
    return True, len(merged) - len(existing)


def main() -> int:
    args = parse_args()

    try:
        local_html, local_entries, local_match = read_market_history(args.local)
        repo_html, repo_entries, repo_match = read_market_history(args.repo)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("Aborting without writing anything -- fix the corrupt file by hand first.", file=sys.stderr)
        return 2

    if local_match is None or repo_match is None:
        print("MARKET_HISTORY block not found in one of the files.", file=sys.stderr)
        return 2

    merged = merge(local_entries, repo_entries)
    merged.sort(key=lambda e: (str(e.get("company", "")), str(e.get("title", ""))))

    local_changed = write_if_changed(args.local, local_html, local_match, merged, local_entries)
    repo_changed = write_if_changed(args.repo, repo_html, repo_match, merged, repo_entries)

    new_for_local = len(merged) - len(local_entries)
    new_for_repo = len(merged) - len(repo_entries)
    print(
        f"Merged: {len(merged)} total entries "
        f"({'local updated, ' if local_changed else ''}+{max(0, new_for_local)} new to local; "
        f"{'repo updated, ' if repo_changed else ''}+{max(0, new_for_repo)} new to repo)"
    )

    tracked_state_path = args.tracked_state or (args.repo.parent / "state" / "tracked_identities.json")
    fresh_local_html = args.local.read_text(encoding="utf-8")
    identities_changed, new_identity_count = sync_tracked_identities(fresh_local_html, tracked_state_path)
    if identities_changed:
        print(f"Synced tracked identities: +{new_identity_count} new (now watching for these to stay hidden from discovery/digest).")
        repo_changed = True

    # DISCARDED_POSTINGS is a newer block -- older local copies may not have
    # it yet, so this is best-effort, not a hard requirement like MARKET_HISTORY.
    # A corrupt block here shouldn't discard the MARKET_HISTORY sync already
    # written above -- warn and skip just this part instead of aborting.
    try:
        local_d_html, local_d_entries, local_d_match = read_discarded(args.local)
        repo_d_html, repo_d_entries, repo_d_match = read_discarded(args.repo)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("Skipping DISCARDED_POSTINGS sync this run; MARKET_HISTORY sync above still applied.", file=sys.stderr)
        local_d_match = repo_d_match = None
    if local_d_match is not None and repo_d_match is not None:
        merged_d = merge_discarded(local_d_entries, repo_d_entries)
        d_local_changed = write_discarded_if_changed(args.local, local_d_html, local_d_match, merged_d, local_d_entries)
        d_repo_changed = write_discarded_if_changed(args.repo, repo_d_html, repo_d_match, merged_d, repo_d_entries)
        if d_local_changed or d_repo_changed:
            print(f"Synced discarded postings: {len(merged_d)} total.")
            repo_changed = repo_changed or d_repo_changed

    print(f"REPO_CHANGED={'1' if repo_changed else '0'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
