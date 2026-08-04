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
SYNCED_TRACKED_PATH = REPO_ROOT / "state" / "tracked_identities.json"
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

# "Remote" alone doesn't mean Chile-eligible -- a lot of Workday postings are
# "<Country> - Remote", meaning remote WITHIN that country's borders (its own
# work-authorization/residency requirements), not open globally. Caught a
# real case of this: a Merck "USA - REMOTE - REMOTE" MSL posting explicitly
# required residing in the US Southeast with up to 50% in-territory travel --
# it passed a naive "remote" substring check and, because compute_fit() only
# looks at the title, scored a perfect 10/10 and got auto-added as a
# HIGH-priority application.
#
# First fix here was a denylist of non-Chile countries -- wrong shape for the
# problem: there are ~195 countries and endless spelling/abbreviation
# variants ("US", "U.S", "US -", "UK", "France", "Japan", "Puerto Rico", ...
# all slipped through a ~14-country denylist in testing). Flipped to an
# allowlist instead: a "remote" location only counts as Chile-viable if it
# actually SAYS something Chile/LATAM/global -- anything else defaults to
# excluded, which is the safe direction to be wrong in (missing a genuinely
# open posting costs nothing; auto-adding one Francisco can't apply to costs
# his time and once fired off a WhatsApp alert about it).
CHILE_VIABLE_REMOTE_SIGNALS = [
    "chile",
    "latam", "latin america", "latinoamerica",
    "sudamerica", "south america",
    "global", "worldwide", "any location", "all locations", "anywhere",
]


def _is_chile_viable_location(location: str) -> bool:
    location_lower = _normalize_for_match(location)
    if "chile" in location_lower:
        return True
    if "remote" not in location_lower:
        return False
    if any(signal in location_lower for signal in CHILE_VIABLE_REMOTE_SIGNALS):
        return True
    # A bare "Remote" with no other qualifier at all (no country name, no
    # region) isn't scoped to anywhere in particular -- every real
    # country-restricted example seen so far has the country named
    # alongside "Remote" ("USA - Remote", "Mexico - Remote", etc.), so an
    # unqualified "Remote" is the one ambiguous case still treated as
    # viable. Anything with extra text alongside "remote" that isn't one of
    # the Chile/LATAM/global signals above is assumed to be naming some
    # other place and excluded.
    return location_lower.strip() == "remote"


# Same failure mode can hide in the TITLE instead of the location field (the
# Merck posting above literally had "(Southeast)" baked into the title) --
# checked independently of location, since the location-based check above
# can't help when location itself is a bare, unscoped "Remote".
US_REGION_TITLE_SIGNALS = [
    "southeast", "northeast", "midwest", "southwest",
    "pacific northwest", "mid-atlantic", "west coast", "east coast",
    "gulf coast", "great lakes", "mountain west", "new england",
    "us citizen", "us citizenship", "citizenship required",
    "security clearance", "us work authorization", "authorized to work in the us",
]


def _has_us_region_restriction(title: str) -> bool:
    lowered = _normalize_for_match(title)
    return any(signal in lowered for signal in US_REGION_TITLE_SIGNALS)

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
    entries = []
    for item in payload.get("results", []):
        # The LinkedIn CLI's own type is `location: string | null` -- a
        # scrape can fail to extract it, giving a JSON `null`, not a missing
        # key, so `.get(..., "")` alone doesn't catch it and `.casefold()`
        # inside the filters below would crash on None.
        location = item.get("location") or ""
        title = item.get("title") or ""
        # LinkedIn's own "Chile" location search (query-scoped via -l) is a
        # soft signal, not a guarantee -- it's known to loosely include
        # globally/remote-tagged postings from other countries. Only
        # second-guess it when we actually have location text to evaluate;
        # if the scraper failed to extract one (location is empty/None, see
        # above), trust the query-level Chile scoping rather than silently
        # dropping an otherwise-legitimate result over a missing field --
        # unlike Workday, LinkedIn has no other source of postings from
        # countries never mentioned in the query at all.
        if location and not _is_chile_viable_location(location):
            continue
        if _has_us_region_restriction(title):
            continue
        entries.append(
            {
                "status": "open",
                "company": item.get("company", ""),
                "title": title,
                "jobId": f"li-{item.get('id', '')}",
                "location": location,
                "firstSeen": stamp,
                "lastConfirmed": stamp,
                "removedOn": "",
                "source": "LinkedIn Jobs",
                "url": item.get("url", ""),
                "details": f"Found via automated LinkedIn search for {query!r}.",
            }
        )
    return entries


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
        title = job.get("title", "")
        # Workday's CxS search has no reliable public location facet, so it
        # returns postings for every country the company hires in. Filter
        # client-side to Chile/remote -- otherwise this floods results with
        # e.g. Malaysia, Uzbekistan, Algeria postings irrelevant to Francisco.
        # "Remote" alone isn't enough (see _is_chile_viable_location): a lot
        # of these are "<Country> - Remote", domestic remote work scoped to
        # that country, not open to a Chile-based candidate.
        if not _is_chile_viable_location(location):
            continue
        if _has_us_region_restriction(title):
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


def read_discarded_postings(html: str) -> list[dict[str, Any]]:
    match = re.search(
        r"// DISCARDED_START\s*\nconst DISCARDED_POSTINGS\s*=\s*(\[[\s\S]*?\])\s*;\s*\n// DISCARDED_END",
        html,
    )
    if not match:
        return []
    return json.loads(match.group(1))


def identity_keys_from_records(records: list[dict[str, Any]]) -> set[str]:
    """Normalized company+role (+ jobId) keys, shared logic for both
    SAVED_DATA (real applications) and DISCARDED_POSTINGS (things Francisco
    said he won't apply to) -- either way, not a "new" discovery worth
    surfacing again."""
    keys = set()
    for record in records:
        company = normalize_key(record.get("company"))
        role = normalize_key(record.get("role"))
        if company and role:
            keys.add(f"{company}|{role}")
        job_id = normalize_key(record.get("jobId"))
        if job_id and re.search(r"\d", job_id):
            keys.add(f"jobid|{job_id}")
    return keys


def tracked_identity_keys(saved_data: list[dict[str, Any]]) -> set[str]:
    """Normalized company+role keys for every application already in
    SAVED_DATA, regardless of status -- a posting we've already applied to,
    interviewed for, or been rejected from is not a "new" discovery, no
    matter what LinkedIn/Workday call it."""
    return identity_keys_from_records(saved_data)


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


SAVED_DATA_PATTERN = re.compile(
    r"(// TRACKER_DATA_START\s*\nconst SAVED_DATA\s*=\s*)(\[[\s\S]*?\])(\s*;\s*\n// TRACKER_DATA_END)"
)


def make_todo_record(entry: dict[str, Any], fit_score: float) -> dict[str, Any]:
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    unique_suffix = abs(hash((entry["company"].casefold(), entry["title"].casefold()))) % 1000
    return {
        "id": f"JOB_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{unique_suffix:03d}",
        "priority": "HIGH",
        "company": entry["company"],
        "companyType": "",
        "role": entry["title"],
        "location": entry.get("location", ""),
        "category": "",
        "jobId": entry.get("jobId", ""),
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
        "links": entry.get("url", ""),
        "strategicNotes": "",
        "jobDescription": entry.get("details", ""),
        "createdAt": today,
    }


def add_todo_records(html: str, records: list[dict[str, Any]]) -> str:
    match = SAVED_DATA_PATTERN.search(html)
    if not match:
        raise RuntimeError("SAVED_DATA block not found in tracker")
    jobs = json.loads(match.group(2))
    jobs.extend(records)
    serialized = json.dumps(jobs, ensure_ascii=False, indent=2)
    return html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]


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
    tracked_keys |= identity_keys_from_records(read_discarded_postings(html))
    if SYNCED_TRACKED_PATH.exists():
        tracked_keys |= set(json.loads(SYNCED_TRACKED_PATH.read_text(encoding="utf-8")))

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
    serialized = json.dumps(merged, ensure_ascii=False, indent=2)
    html = html[: match.start()] + match.group(1) + serialized + match.group(3) + html[match.end() :]

    high_fit = [(e, compute_fit(e["title"])) for e in new_entries]
    high_fit = [(e, score) for e, score in high_fit if score >= HIGH_FIT_THRESHOLD]
    high_fit.sort(key=lambda pair: pair[1], reverse=True)

    print(f"Added {len(new_entries)} new posting(s), {len(high_fit)} high-fit (>= {HIGH_FIT_THRESHOLD}).")

    if high_fit:
        todo_records = [make_todo_record(entry, score) for entry, score in high_fit]
        html = add_todo_records(html, todo_records)
        print(f"Auto-added {len(todo_records)} high-fit posting(s) to 'Por postular'.")

    TRACKER_PATH.write_text(html, encoding="utf-8")

    if high_fit:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = set()
        if STATE_PATH.exists():
            state = set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        for entry, _ in high_fit:
            state.add(entry_key(entry))
        STATE_PATH.write_text(json.dumps(sorted(state), ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [f"Alerta: {len(high_fit)} puesto(s) nuevo(s) de alto fit, agregados a Por postular:"]
        for entry, score in high_fit[:5]:
            lines.append(f"- {entry['company']}: {entry['title']} (fit {score:.0f}/10) {entry['url']}")
        sys.argv = ["send_whatsapp.py", "\n".join(lines)]
        send_whatsapp_main()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
