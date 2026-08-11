#!/usr/bin/env python3

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_monitor_summary import summary_from_reports


REPORTS_DIR = Path("/Users/franciscokirhman/Documents/Codex/2026-07-24/s/outputs/chile_careers_reports")


class MonitorSummaryTests(unittest.TestCase):
    def test_latest_overnight_attempts(self) -> None:
        summary = summary_from_reports(REPORTS_DIR)
        self.assertEqual(summary["configuredSources"], 25)
        self.assertEqual(summary["inventoryAttempts"], 2)
        self.assertEqual(summary["recoveryAttempts"], 25)
        self.assertEqual(summary["totalAttempts"], 27)
        self.assertEqual(summary["comparable"], 2)
        self.assertEqual(summary["unresolved"], 23)


if __name__ == "__main__":
    unittest.main()
