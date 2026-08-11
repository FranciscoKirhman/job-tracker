#!/usr/bin/env python3
"""Watch official OpenAI Community references for Codex usage resets.

The Codex team has historically announced global resets through official staff
posts, sometimes on X before the announcement is mirrored in the OpenAI
Developer Community. This watcher uses the public OpenAI Community RSS and
search endpoints, and only keeps candidates that contain reset language,
Codex/ChatGPT Work scope, and an official OpenAI staff signal such as
@thsottiaux or OpenAI_Support.

State is split into ``pending`` and ``seen`` records. The workflow commits a
pending record before sending WhatsApp, then moves it to ``seen`` only after
the send succeeds. This makes a failed send retryable without exposing any
credentials or duplicating successful alerts during normal runs.
"""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree


ANNOUNCEMENTS_RSS = "https://community.openai.com/c/announcements/6.rss"
SEARCH_URL = "https://community.openai.com/search.json?q={}"
SEARCH_QUERIES = (
    "Codex rate limit reset",
    "Codex usage limits reset",
    "ChatGPT Work usage limit reset",
)
USER_AGENT = "jobs3-openai-usage-reset-watch/1.0"
DEFAULT_STATE = Path("state/openai_usage_reset_watch.json")
MAX_RECORDS = 250
MAX_ALERTS_PER_MESSAGE = 3

RESET_RE = re.compile(r"\breset(?:s|ting|ted)?\b|rate[ -]?limit reset", re.I)
LIMIT_RE = re.compile(r"rate[ -]?limit|usage limit|usage limits|quota", re.I)
SCOPE_RE = re.compile(r"codex|chatgpt\s+work|work\s+and\s+codex", re.I)
X_POST_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/thsottiaux/status/\d+", re.I)
OFFICIAL_COMMUNITY_USERS = {"openai_support", "thsottiaux"}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


class WatchError(RuntimeError):
    """Raised when an official source cannot be read safely."""


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, application/xml"})
    try:
        with urlopen(request, timeout=25) as response:
            return response.read()
    except Exception as exc:  # pragma: no cover - exercised by the live workflow
        raise WatchError(f"official source failed: {url}: {exc}") from exc


def fetch_json(url: str) -> dict[str, Any]:
    try:
        value = json.loads(fetch_bytes(url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchError(f"official source returned invalid JSON: {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatchError(f"official source returned non-object JSON: {url}")
    return value


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def is_reset_candidate(
    text: str,
    username: str | None = None,
    verified_x_post: bool = False,
) -> bool:
    """Return true only for a scoped, official-looking reset announcement."""

    official_signal = verified_x_post or (
        isinstance(username, str) and username.casefold() in OFFICIAL_COMMUNITY_USERS
    )
    return bool(
        RESET_RE.search(text)
        and LIMIT_RE.search(text)
        and SCOPE_RE.search(text)
        and official_signal
    )


def x_post_url(text: str) -> str | None:
    match = X_POST_RE.search(text)
    return match.group(0) if match else None


def verify_x_post(status_url: str) -> bool:
    """Confirm through X oEmbed that the status really belongs to @thsottiaux."""

    endpoint = "https://publish.twitter.com/oembed?omit_script=true&url=" + quote(status_url, safe="")
    payload = fetch_json(endpoint)
    author_url = str(payload.get("author_url") or "")
    canonical_url = str(payload.get("url") or "")
    return bool(
        re.fullmatch(r"https://(?:x\.com|twitter\.com)/thsottiaux/?", author_url, re.I)
        and X_POST_RE.fullmatch(canonical_url)
    )


def verified_x_post_url(text: str) -> str | None:
    status_url = x_post_url(text)
    if status_url and verify_x_post(status_url):
        return status_url
    return None


def candidate(
    *,
    key: str,
    title: str,
    url: str,
    published_at: str | None,
    excerpt: str,
    source: str,
    official_post: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "key": key,
        "title": clean_text(title) or "OpenAI usage reset announcement",
        "url": url,
        "published_at": published_at,
        "excerpt": clean_text(excerpt)[:900],
        "source": source,
    }
    if official_post:
        record["official_post"] = official_post
    return record


def parse_rss(xml_bytes: bytes) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise WatchError(f"official RSS returned invalid XML: {exc}") from exc

    records: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = item.findtext("title") or ""
        description = item.findtext("description") or ""
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or link
        creator = item.findtext("{http://purl.org/dc/elements/1.1/}creator")
        text = f"{title} {description}"
        official_post = verified_x_post_url(text) if X_POST_RE.search(text) else None
        if link and is_reset_candidate(text, creator, bool(official_post)):
            records.append(
                candidate(
                    key=f"rss:{guid}",
                    title=title,
                    url=link,
                    published_at=parse_date(item.findtext("pubDate")),
                    excerpt=description,
                    source="OpenAI Developer Community announcements RSS",
                    official_post=official_post,
                )
            )
    return records


def topic_title(topic_id: int) -> str | None:
    """Fetch the topic title when a search result omits it."""

    url = f"https://community.openai.com/t/{topic_id}.json"
    try:
        payload = fetch_json(url)
    except WatchError:
        return None
    title = payload.get("title")
    return clean_text(title) if isinstance(title, str) else None


def parse_search_payload(payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    posts = payload.get("posts", [])
    if not isinstance(posts, list):
        return records
    for post in posts:
        if not isinstance(post, dict):
            continue
        post_id = post.get("id")
        topic_id = post.get("topic_id")
        if not isinstance(post_id, int) or not isinstance(topic_id, int):
            continue
        excerpt = clean_text(str(post.get("blurb") or ""))
        username = str(post.get("username") or "") or None
        official_post = verified_x_post_url(excerpt) if X_POST_RE.search(excerpt) else None
        if not is_reset_candidate(excerpt, username, bool(official_post)):
            continue
        url = f"https://community.openai.com/t/{topic_id}"
        title = topic_title(topic_id) or excerpt.split("...")[0][:160]
        records.append(
            candidate(
                key=f"community-post:{post_id}",
                title=title,
                url=url,
                published_at=str(post.get("created_at") or "") or None,
                excerpt=excerpt,
                source=f"OpenAI Developer Community search: {query}",
                official_post=official_post,
            )
        )
    return records


def dedupe(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("key") or "")
        if key:
            result[key] = record
    return list(result.values())


def collect_candidates() -> list[dict[str, Any]]:
    records = parse_rss(fetch_bytes(ANNOUNCEMENTS_RSS))
    for query in SEARCH_QUERIES:
        payload = fetch_json(SEARCH_URL.format(quote(query)))
        records.extend(parse_search_payload(payload, query))
    return sorted(
        dedupe(records),
        key=lambda record: str(record.get("published_at") or ""),
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "initialized": False, "seen": {}, "pending": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchError(f"invalid watcher state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatchError(f"invalid watcher state {path}: expected an object")
    value.setdefault("version", 1)
    value.setdefault("initialized", False)
    value.setdefault("seen", {})
    value.setdefault("pending", {})
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def trim_seen_records(records: dict[str, Any]) -> dict[str, Any]:
    items = list(records.items())
    items.sort(key=lambda pair: str(pair[1].get("published_at") or ""))
    return dict(items[-MAX_RECORDS:])


def render_message(records: Iterable[dict[str, Any]]) -> str:
    records = list(records)
    if len(records) == 1 and records[0].get("message"):
        return str(records[0]["message"]).strip()[:4096]
    lines = ["🚨 Nuevo anuncio de reset de límites de ChatGPT Work/Codex"]
    for record in records:
        lines.append("")
        lines.append(f"• {record['title']}")
        if record.get("published_at"):
            lines.append(f"  Publicado: {record['published_at']}")
        lines.append(f"  Fuente: {record['url']}")
        if record.get("official_post"):
            lines.append(f"  Post oficial citado: {record['official_post']}")
        excerpt = clean_text(str(record.get("excerpt") or ""))
        if excerpt:
            lines.append(f"  {excerpt[:500]}")
    return "\n".join(lines)[:4096]


def write_outputs(notify: bool, message: str = "", keys: Iterable[str] = ()) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    marker = f"OPENAI_RESET_MESSAGE_{uuid4().hex}"
    while marker in message:
        marker = f"OPENAI_RESET_MESSAGE_{uuid4().hex}"
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"notify={'true' if notify else 'false'}\n")
        output.write(f"keys_json={json.dumps(list(keys), separators=(',', ':'))}\n")
        output.write(f"message<<{marker}\n")
        output.write(message)
        output.write(f"\n{marker}\n")


def write_pending_outputs(state: dict[str, Any]) -> None:
    pending_records = sorted(
        state["pending"].values(),
        key=lambda record: str(record.get("published_at") or ""),
    )
    if pending_records:
        batch = pending_records[:MAX_ALERTS_PER_MESSAGE]
        message = render_message(batch)
        keys = [str(record["key"]) for record in batch]
        print(f"{len(pending_records)} pending OpenAI usage reset alert(s); sending {len(batch)}.")
        write_outputs(True, message, keys)
    else:
        print("No new OpenAI usage reset announcements.")
        write_outputs(False)


def known_official_posts(state: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for bucket_name in ("seen", "pending"):
        bucket = state.get(bucket_name, {})
        if isinstance(bucket, dict):
            for record in bucket.values():
                if isinstance(record, dict) and record.get("official_post"):
                    urls.add(str(record["official_post"]))
    return urls


def scan(path: Path) -> int:
    state = load_state(path)
    candidates = collect_candidates()

    if not state["initialized"]:
        pending = state.get("pending", {})
        if not isinstance(pending, dict):
            raise WatchError("watcher state has invalid pending records")
        state["initialized"] = True
        state["seen"] = trim_seen_records({record["key"]: record for record in candidates})
        state["pending"] = pending
        save_state(path, state)
        print(f"Bootstrapped from {len(candidates)} existing candidate(s); no alert sent.")
        write_outputs(False)
        return 0

    seen = state.get("seen", {})
    pending = state.get("pending", {})
    if not isinstance(seen, dict) or not isinstance(pending, dict):
        raise WatchError("watcher state has invalid seen or pending records")

    official_posts = known_official_posts(state)
    for record in candidates:
        key = record["key"]
        official_post = str(record.get("official_post") or "")
        if key not in seen and key not in pending and (not official_post or official_post not in official_posts):
            pending[key] = record
            if official_post:
                official_posts.add(official_post)

    state["pending"] = pending
    state["seen"] = trim_seen_records(seen)
    save_state(path, state)

    write_pending_outputs(state)
    return 0


def ingest_luna_report(
    path: Path,
    official_url: str,
    title: str,
    published_at: str | None,
    message: str,
) -> int:
    """Queue one Luna-researched official X event into the shared outbox."""

    official_url = official_url.strip()
    if not X_POST_RE.fullmatch(official_url) or not verify_x_post(official_url):
        raise WatchError("Luna ingest URL is not a verified @thsottiaux X status")

    state = load_state(path)
    seen = state.get("seen", {})
    pending = state.get("pending", {})
    if not isinstance(seen, dict) or not isinstance(pending, dict):
        raise WatchError("watcher state has invalid seen or pending records")
    state["initialized"] = True

    if official_url in known_official_posts(state):
        print(f"Official event already recorded: {official_url}")
        save_state(path, state)
        write_outputs(False)
        return 0

    status_id = official_url.rstrip("/").rsplit("/", 1)[-1]
    record = candidate(
        key=f"x-status:{status_id}",
        title=title or "Luna-confirmed OpenAI usage reset",
        url=official_url,
        published_at=published_at,
        excerpt=message,
        source="Luna official-source research; X author verified by oEmbed",
        official_post=official_url,
    )
    if message.strip():
        record["message"] = message.strip()[:4096]
    pending[record["key"]] = record
    state["pending"] = pending
    state["seen"] = trim_seen_records(seen)
    save_state(path, state)
    write_pending_outputs(state)
    return 0


def finalize(path: Path, keys: Iterable[str] | None = None) -> int:
    state = load_state(path)
    pending = state.get("pending", {})
    seen = state.get("seen", {})
    if not isinstance(pending, dict) or not isinstance(seen, dict):
        raise WatchError("watcher state has invalid seen or pending records")
    keys_to_finalize = list(pending) if keys is None else list(keys)
    finalized = 0
    for key in keys_to_finalize:
        if key in pending:
            seen[key] = pending.pop(key)
            finalized += 1
    state["seen"] = trim_seen_records(seen)
    state["pending"] = pending
    save_state(path, state)
    print(f"Marked {finalized} OpenAI usage reset alert(s) as sent.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--finalize", action="store_true", help="move pending alerts to seen after WhatsApp succeeds")
    parser.add_argument("--finalize-keys-json", help="JSON list of pending keys included in the successful WhatsApp send")
    parser.add_argument("--ingest-url", help="verified official X status found by the Luna researcher")
    parser.add_argument("--ingest-title", default="")
    parser.add_argument("--ingest-published-at")
    parser.add_argument("--ingest-message", default="")
    args = parser.parse_args()
    try:
        keys: list[str] | None = None
        if args.finalize_keys_json:
            parsed_keys = json.loads(args.finalize_keys_json)
            if not isinstance(parsed_keys, list) or not all(isinstance(key, str) for key in parsed_keys):
                raise WatchError("--finalize-keys-json must be a JSON list of strings")
            keys = parsed_keys
        if args.finalize:
            return finalize(args.state, keys)
        if args.ingest_url:
            return ingest_luna_report(
                args.state,
                args.ingest_url,
                args.ingest_title,
                args.ingest_published_at,
                args.ingest_message,
            )
        return scan(args.state)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid --finalize-keys-json: {exc}", file=sys.stderr)
        return 1
    except WatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
