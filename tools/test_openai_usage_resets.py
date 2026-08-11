#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from check_openai_usage_resets import (
    finalize,
    ingest_luna_report,
    is_reset_candidate,
    load_state,
    parse_rss,
    parse_search_payload,
    scan,
    verify_x_post,
    write_outputs,
)


class OpenAIUsageResetWatchTests(unittest.TestCase):
    def test_accepts_community_reference_to_official_codex_reset(self):
        text = (
            "The next rate limit reset has already been announced for Monday. "
            "I have reset usage limits for all paid users of ChatGPT Work and Codex. "
            "https://x.com/thsottiaux/status/2086188036493344823"
        )
        self.assertTrue(is_reset_candidate(text, verified_x_post=True))

    def test_rejects_unscoped_or_unofficial_usage_discussion(self):
        self.assertFalse(is_reset_candidate("Reset the local test counter."))
        self.assertFalse(is_reset_candidate("Codex usage limits are confusing for me."))
        self.assertFalse(is_reset_candidate("OpenAI Support reset a password."))
        self.assertFalse(is_reset_candidate("Tibo announced a Codex rate limit reset."))

    def test_accepts_verified_openai_support_author(self):
        self.assertTrue(
            is_reset_candidate(
                "Codex usage limits were reset for paid plans.",
                "OpenAI_Support",
            )
        )

    def test_rss_candidate_parses_utc_date(self):
        rss = b"""<?xml version="1.0"?>
        <rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel><item>
            <title>Codex rate limit reset</title>
            <link>https://community.openai.com/t/123</link>
            <guid>topic-123</guid>
            <pubDate>Sat, 08 Aug 2026 20:29:00 +0000</pubDate>
            <dc:creator>VeitB</dc:creator>
            <description>ChatGPT Work and Codex usage limits reset. https://x.com/thsottiaux/status/2086188036493344823</description>
          </item></channel>
        </rss>"""
        with patch("check_openai_usage_resets.verify_x_post", return_value=True):
            records = parse_rss(rss)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["published_at"], "2026-08-08T20:29:00+00:00")

    def test_search_result_becomes_stable_candidate(self):
        payload = {
            "posts": [
                {
                    "id": 1927392,
                    "topic_id": 1389643,
                    "created_at": "2026-08-08T22:23:14.736Z",
                    "blurb": (
                        "Codex rate limits: the next rate limit reset has already been announced for Monday. "
                        "Tibo https://x.com/thsottiaux/status/2086188036493344823"
                    ),
                }
            ]
        }
        with (
            patch("check_openai_usage_resets.topic_title", return_value="Codex rate limits reset"),
            patch("check_openai_usage_resets.verify_x_post", return_value=True),
        ):
            records = parse_search_payload(payload, "Codex rate limit reset")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key"], "community-post:1927392")
        self.assertEqual(records[0]["url"], "https://community.openai.com/t/1389643")

    def test_pending_records_survive_batching_and_finalize_only_sent_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            initial = {"key": "community-post:1", "title": "Initial", "url": "https://community.openai.com/t/1", "published_at": "2026-08-01", "excerpt": "", "source": "test"}
            with patch("check_openai_usage_resets.collect_candidates", return_value=[initial]):
                self.assertEqual(scan(state_path), 0)

            new_records = []
            for number in range(2, 257):
                new_records.append({"key": f"community-post:{number}", "title": f"Reset {number}", "url": f"https://community.openai.com/t/{number}", "published_at": f"2026-08-{number % 28 + 1:02d}", "excerpt": "", "source": "test"})
            with patch("check_openai_usage_resets.collect_candidates", return_value=new_records):
                self.assertEqual(scan(state_path), 0)

            state = load_state(state_path)
            self.assertEqual(len(state["pending"]), 255)
            sent_key = sorted(state["pending"])[0]
            self.assertEqual(finalize(state_path, [sent_key]), 0)
            state = load_state(state_path)
            self.assertNotIn(sent_key, state["pending"])
            self.assertEqual(len(state["pending"]), 254)
            self.assertIn(sent_key, state["seen"])

    def test_github_output_uses_unpredictable_delimiter_and_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output.txt"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_path)}):
                write_outputs(True, "contains OPENAI_RESET_MESSAGE_EOF safely", ["community-post:1"])
            output = output_path.read_text()
            self.assertIn('keys_json=["community-post:1"]', output)
            self.assertNotIn("message<<OPENAI_RESET_MESSAGE_EOF\n", output)

    def test_x_post_verification_requires_canonical_author(self):
        status_url = "https://x.com/thsottiaux/status/2086188036493344823"
        with patch(
            "check_openai_usage_resets.fetch_json",
            return_value={"author_url": "https://x.com/thsottiaux", "url": status_url},
        ):
            self.assertTrue(verify_x_post(status_url))
        with patch(
            "check_openai_usage_resets.fetch_json",
            return_value={"author_url": "https://x.com/someone_else", "url": status_url},
        ):
            self.assertFalse(verify_x_post(status_url))

    def test_luna_ingest_and_community_scan_share_official_url_dedupe(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            status_url = "https://x.com/thsottiaux/status/2086972802457063486"
            with patch("check_openai_usage_resets.verify_x_post", return_value=True):
                self.assertEqual(
                    ingest_luna_report(
                        state_path,
                        status_url,
                        "Monday reset completed",
                        "2026-08-10T20:27:00-04:00",
                        "Informe Luna",
                    ),
                    0,
                )
            state = load_state(state_path)
            self.assertEqual(len(state["pending"]), 1)
            sent_key = next(iter(state["pending"]))
            self.assertEqual(finalize(state_path, [sent_key]), 0)

            mirrored = {
                "key": "community-post:999",
                "title": "Community mirror",
                "url": "https://community.openai.com/t/999",
                "published_at": "2026-08-11T00:00:00Z",
                "excerpt": "",
                "source": "test",
                "official_post": status_url,
            }
            with patch("check_openai_usage_resets.collect_candidates", return_value=[mirrored]):
                self.assertEqual(scan(state_path), 0)
            state = load_state(state_path)
            self.assertEqual(state["pending"], {})


if __name__ == "__main__":
    unittest.main()
