#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from publish_canonical_snapshot import publish


def tracker(records: list[dict], suffix: str = "") -> str:
    return (
        "<script>\n// TRACKER_DATA_START\nconst SAVED_DATA = "
        + json.dumps(records)
        + ";\n// TRACKER_DATA_END\n"
        + suffix
        + "</script>"
    )


class PublishCanonicalSnapshotTests(unittest.TestCase):
    def test_publishes_only_saved_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical = Path(temp_dir) / "canonical.html"
            repo = Path(temp_dir) / "repo.html"
            canonical.write_text(tracker([{"jobId": "1", "status": "rejected"}], "LOCAL"))
            repo.write_text(tracker([{"jobId": "1", "status": "applied"}], "REPO"))

            changed, canonical_count, repo_count = publish(canonical, repo)

            self.assertTrue(changed)
            self.assertEqual((canonical_count, repo_count), (1, 1))
            self.assertIn('"status": "rejected"', repo.read_text())
            self.assertIn("REPO", repo.read_text())
            self.assertIn("LOCAL", canonical.read_text())

    def test_noop_when_records_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            canonical = Path(temp_dir) / "canonical.html"
            repo = Path(temp_dir) / "repo.html"
            payload = tracker([{"jobId": "1", "status": "applied"}])
            canonical.write_text(payload)
            repo.write_text(payload)
            self.assertEqual(publish(canonical, repo), (False, 1, 1))


if __name__ == "__main__":
    unittest.main()
