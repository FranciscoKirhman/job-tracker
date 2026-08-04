#!/usr/bin/env python3
"""Discover new job postings from LinkedIn + Workday and merge into MARKET_HISTORY.

No LLM/agent calls -- plain HTTP + a keyword-overlap fit heuristic, to avoid
API costs beyond the Claude.ai subscription Francisco already pays for.

Sources:
  - LinkedIn (tools/linkedin-search, vendored from MadsLorentzen/ai-job-search,
    MIT licensed): public jobs-guest endpoints, no auth. The upstream skill's
    own README notes automated access is against LinkedIn's ToS -- kept to a
    handful of queries per run, personal use only, per that same guidance.
  - Workday CxS API: the same JSON endpoint a company's own public careers
    site JS calls (no auth, not an explicit ToS violation like LinkedIn, but
    also not a published partner API -- lower risk, not zero). Currently only
    configured for Abbott (site ID confirmed by hand); add more companies to
    WORKDAY_SOURCES as their tenant/site IDs are discovered.

High-fit finds (score >= HIGH_FIT_THRESHOLD) get an immediate WhatsApp alert
and are marked "seen" in state/market_history_seen.json right away, so the
next scheduled digest doesn't repeat them. Everything else just lands in
MARKET_HISTORY for the regular daily digest to report.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_whatsapp import main as send_whatsapp_main  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKER_PATH = REPO_ROOT / "francisco-job-tracker-2026.html"
STATE_PATH = REPO_ROOT / "state" / "market_history_seen.json"
LINKEDIN_CLI = REPO_ROOT / "tools" / "linkedin-search" / "cli" / "src" / "cli.ts"
MARKET_HISTORY_PATTERN = re.compile(r"(const MARKET_HISTORY = )(\[.*?\])(;)", re.S)

# Kept short deliberately -- "keep volume low" per the linkedin-search skill's
# own ToS caution. Derived from the category values already present in
# Francisco's own SAVED_DATA, not guessed.
LINKEDIN_QUERIES = [
    "Medical Science Liaison",
    "Medical Affairs",
    "Clinical Research",
    "Regulatory Affairs",
    "Clinical Trial",
]
LINKEDIN_LOCATION = "Chile"
LINKEDIN_JOBAGE_DAYS = 7

# Workday tenant/site pairs confirmed by hand (curl). Add more as they're
# discovered -- try the pattern in docs/JOB_DISCOVERY.md.
WORKDAY_SOURCES = [
    {"company": "Abbott", "tenant": "abbott", "wd": "wd5", "site": "abbottcareers"},
    {"company": "Pfizer", "tenant": "pfizer", "wd": "wd1", "site": "PfizerCareers"},
    {"company": "Merck (MSD)", "tenant": "msd", "wd": "wd5", "site": "SearchJobs"},
    {"company": "AstraZeneca", "tenant": "astrazeneca", "wd": "wd3", "site": "Careers"},
    {"company": "Sanofi", "tenant": "sanofi", "wd": "wd3", "site": "SanofiCareers"},
]
WORKDAY_KEYWORDS = ["Medical Science Liaison", "Medical Affairs", "Clinical", "Regulatory"]

FIT_KEYWORDS = {
    "medical science liaison": 4,
    "medical affairs": 3,
    "asuntos medicos": 3,
    "clinical research": 3,
    "investigacion clinica": 3,
    "clinical trial": 2,
    "ensayo clinico": 2,
    "ensayos clinicos": 2,
    "regulatory affairs": 3,
    "asuntos regulatorios": 3,
    "regulatory": 1,
    "regulatorio": 1,
    "biotechnology": 2,
    "biotecnolog": 2,
    "gcp": 2,
    "pharma": 1,
    "farmaceutic": 1,
    "diagnostic": 1,
    "diagnostico": 1,
    "oncology": 1,
    "oncologia": 1,
    "chile": 1,
    "santiago": 1,
    "remote": 1,
    "remoto": 1,
}
HIGH_FIT_THRESHOLD = 7


def _normalize_for_match(text: str) -> str:
    """casefold + strip accents, so 'oncología' matches 'oncologia', etc."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


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


def compute_fit(title: str) -> float:
    lowered = _normalize_for_match(title)
    score = 0.0
    # Any "<medical/scientific/science> liaison" title, or the bare "MSL"
    # abbreviation as a whole word, is a near-exact match for Francisco's core
    # target role regardless of exact wording -- always push past the
    # high-fit threshold, independent of the additive keyword scoring below.
    has_liaison_phrase = "liaison" in lowered and any(
        word in lowered for word in ("medical", "scientific", "science")
    )
    has_msl_abbrev = re.search(r"\bmsl\b", lowered) is not None
    if has_liaison_phrase or has_msl_abbrev:
        score = 8.0
    score += sum(weight for keyword, weight in FIT_KEYWORDS.items() if keyword in lowered)
    return min(10.0, score)


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def fetch_linkedin(query: str, location: str) -> list[dict[str, Any]]:
    if not LINKEDIN_CLI.exists():
        print(f"LinkedIn CLI not found at {LINKEDIN_CLI}", file=sys.stderr)
        return []
    try:
        result = subprocess.run(
            [
                "bun", "run", str(LINKEDIN_CLI), "search",
                "-q", query, "-l", location,
                "--jobage", str(LINKEDIN_JOBAGE_DAYS),
                "--format", "json",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        payload = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"LinkedIn search failed for {query!r}: {exc}", file=sys.stderr)
        return []

    stamp = now_stamp()
    return [
        {
            "status": "open",
            "company": item.get("company", ""),
            "title": item.get("title", ""),
            "jobId": f"li-{item.get('id', '')}",
            "location": item.get("location", ""),
            "firstSeen": stamp,
            "lastConfirmed": stamp,
            "removedOn": "",
            "source": "LinkedIn Jobs",
            "url": item.get("url", ""),
            "details": f"Found via automated LinkedIn search for {query!r}.",
        }
        for item in payload.get("results", [])
    ]


def fetch_workday(source: dict[str, str], keyword: str) -> list[dict[str, Any]]:
    url = f"https://{source['tenant']}.{source['wd']}.myworkdayjobs.com/wday/cxs/{source['tenant']}/{source['site']}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": keyword}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Workday search failed for {source['company']} / {keyword!r}: {exc}", file=sys.stderr)
        return []

    stamp = now_stamp()
    entries = []
    for job in payload.get("jobPostings", []):
        location = job.get("locationsText", "")
        # Workday's CxS search has no reliable public location facet, so it
        # returns postings for every country the company hires in. Filter
        # client-side to Chile/remote -- otherwise this floods results with
        # e.g. Malaysia, Uzbekistan, Algeria postings irrelevant to Francisco.
        location_lower = location.casefold()
        if "chile" not in location_lower and "remote" not in location_lower:
            continue
        path = job.get("externalPath", "")
        entries.append(
            {
                "status": "open",
                "company": source["company"],
                "title": job.get("title", ""),
                "jobId": f"wd-{source['tenant']}-{path.rsplit('_', 1)[-1] if '_' in path else path}",
                "location": location,
                "firstSeen": stamp,
                "lastConfirmed": stamp,
                "removedOn": "",
                "source": "Workday careers site",
                "url": f"https://{source['tenant']}.{source['wd']}.myworkdayjobs.com/{source['site']}{path}",
                "details": f"Found via automated Workday search for {keyword!r}.",
            }
        )
    return entries


def read_market_history() -> tuple[str, list[dict[str, Any]], re.Match[str]]:
    html = TRACKER_PATH.read_text(encoding="utf-8")
    match = MARKET_HISTORY_PATTERN.search(html)
    if not match:
        raise RuntimeError("MARKET_HISTORY block not found in tracker")
    return html, json.loads(match.group(2)), match


def read_saved_data(html: str) -> list[dict[str, Any]]:
    match = re.search(
        r"// TRACKER_DATA_START\s*\nconst SAVED_DATA\s*=\s*(\[[\s\S]*?\])\s*;\s*\n// TRACKER_DATA_END",
        html,
    )
    if not match:
        return []
    return json.loads(match.group(1))


def tracked_identity_keys(saved_data: list[dict[str, Any]]) -> set[str]:
    """Normalized company+role keys for every application already in
    SAVED_DATA, regardless of status -- a posting we've already applied to,
    interviewed for, or been rejected from is not a "new" discovery, no
    matter what LinkedIn/Workday call it."""
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


def already_tracked(entry: dict[str, Any], tracked_keys: set[str]) -> bool:
    company = normalize_key(entry.get("company"))
    role = normalize_key(entry.get("title"))
    if company and role and f"{company}|{role}" in tracked_keys:
        return True
    # Discovered jobId is prefixed ("li-4441683250", "wd-abbott-31157043-1");
    # match if the tracked numeric/alnum core ID appears as a substring.
    raw_id = normalize_key(entry.get("jobId"))
    for key in tracked_keys:
        if key.startswith("jobid|"):
            tracked_id = key[len("jobid|"):]
            if tracked_id and tracked_id in raw_id:
                return True
    return False


def write_market_history(html: str, match: re.Match[str], entries: list[dict[str, Any]]) -> None:
    serialized = json.dumps(entries, ensure_ascii=False, indent=2)
    new_html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]
    TRACKER_PATH.write_text(new_html, encoding="utf-8")


def main() -> int:
    found: list[dict[str, Any]] = []
    for query in LINKEDIN_QUERIES:
        found.extend(fetch_linkedin(query, LINKEDIN_LOCATION))
    for source in WORKDAY_SOURCES:
        for keyword in WORKDAY_KEYWORDS:
            found.extend(fetch_workday(source, keyword))

    html, existing, match = read_market_history()
    existing_keys = {entry_key(e) for e in existing}
    tracked_keys = tracked_identity_keys(read_saved_data(html))

    new_entries = []
    already_tracked_count = 0
    seen_this_run = set()
    for entry in found:
        key = entry_key(entry)
        if key in existing_keys or key in seen_this_run:
            continue
        if already_tracked(entry, tracked_keys):
            already_tracked_count += 1
            continue
        seen_this_run.add(key)
        new_entries.append(entry)

    if already_tracked_count:
        print(f"Skipped {already_tracked_count} posting(s) already in SAVED_DATA (applied/rejected/interview/etc).")

    if not new_entries:
        print("No new postings found.")
        return 0

    merged = existing + new_entries
    write_market_history(html, match, merged)

    high_fit = [(e, compute_fit(e["title"])) for e in new_entries]
    high_fit = [(e, score) for e, score in high_fit if score >= HIGH_FIT_THRESHOLD]
    high_fit.sort(key=lambda pair: pair[1], reverse=True)

    print(f"Added {len(new_entries)} new posting(s), {len(high_fit)} high-fit (>= {HIGH_FIT_THRESHOLD}).")

    if high_fit:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = set()
        if STATE_PATH.exists():
            state = set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        for entry, _ in high_fit:
            state.add(entry_key(entry))
        STATE_PATH.write_text(json.dumps(sorted(state), ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [f"Alerta: {len(high_fit)} puesto(s) nuevo(s) de alto fit:"]
        for entry, score in high_fit[:5]:
            lines.append(f"- {entry['company']}: {entry['title']} (fit {score:.0f}/10) {entry['url']}")
        sys.argv = ["send_whatsapp.py", "\n".join(lines)]
        send_whatsapp_main()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
