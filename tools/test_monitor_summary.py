#!/usr/bin/env python3

import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_monitor_summary import embed_summary, summary_from_reports


REPORTS_DIR = Path("/Users/franciscokirhman/Documents/Codex/2026-07-24/s/outputs/chile_careers_reports")


class MonitorSummaryTests(unittest.TestCase):
    def test_latest_monitor_attempts(self) -> None:
        summary = summary_from_reports(REPORTS_DIR)
        self.assertEqual(summary["configuredSources"], 25)
        self.assertEqual(
            summary["inventoryAttempts"],
            summary["attemptEvidence"]["initialSourceAttempts"]
            + summary["attemptEvidence"]["inventoryRetries"],
        )
        self.assertEqual(
            summary["recoveryAttempts"],
            summary["attemptEvidence"]["recoverySourceAttempts"],
        )
        self.assertEqual(
            summary["totalAttempts"],
            summary["inventoryAttempts"] + summary["recoveryAttempts"],
        )
        self.assertEqual(
            summary["comparable"] + summary["unresolved"],
            summary["configuredSources"],
        )
        self.assertEqual(len(summary["sources"]), summary["configuredSources"])
        self.assertEqual(
            len(summary["failedSources"]) + len(summary["reviewedSources"]),
            summary["configuredSources"],
        )
        expected_checked = summary["recoveryChecked"] or summary["inventoryChecked"]
        self.assertTrue(all(source["checked"] == expected_checked for source in summary["sources"]))
        self.assertTrue(all(source["url"].startswith("https://") for source in summary["sources"]))

    def test_embedded_snapshot_updates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = Path(temp_dir) / "tracker.html"
            tracker.write_text("<script>const LINKEDIN_DISCOVERY_SOURCE={};</script>", encoding="utf-8")
            self.assertTrue(embed_summary(tracker, {"unresolved": 23}))
            self.assertFalse(embed_summary(tracker, {"unresolved": 23}))
            self.assertTrue(embed_summary(tracker, {"unresolved": 22}))
            html = tracker.read_text(encoding="utf-8")
            self.assertEqual(html.count("// MONITOR_SUMMARY_START"), 1)
            self.assertIn('"unresolved": 22', html)


if __name__ == "__main__":
    unittest.main()
