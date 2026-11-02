"""Regression coverage for sent follow-ups and preserved application history."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from follow_up import follow_up_fields, validate_follow_up_order
from tracker_context import latest_activity
from update_job_tracker import make_record, parse_template, merge_record, read_tracker


class FollowUpTests(unittest.TestCase):
    def test_requires_date_and_source_and_rejects_scheduled_mail(self):
        for day, evidence in [("2026-10-30", ""), ("", "https://mail.google.com/mail/#sent/1"), ("2999-01-01", "https://mail.google.com/mail/#sent/1")]:
            with self.assertRaises(ValueError):
                follow_up_fields(day, evidence)

    def test_does_not_replace_newer_followup(self):
        with self.assertRaises(ValueError):
            validate_follow_up_order({"lastFollowUpDate": "2026-10-30"}, {"lastFollowUpDate": "2026-10-29"})

    def test_structured_import_reads_followup_without_assessment_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "posting.txt"
            path.write_text("COMPANY: Example\nROLE: Scientist\nJOB_ID: J-123\nFOLLOW_UP_DATE: 2026-10-30\nFOLLOW_UP_EVIDENCE: https://mail.google.com/mail/#sent/1\n")
            fields, sections = parse_template(path)
            incoming = make_record(path, fields, sections, None, None, False)
            self.assertEqual(incoming['lastFollowUpDate'], '2026-10-30')
            existing = {"id": "one", "createdAt": "2026-10-01", "status": "interview", "replyDate": "2026-10-19", "interviews": [{"date": "2026-10-19"}]}
            incoming['status'] = existing['status']
            merged = merge_record(existing, incoming)
            self.assertEqual(merged['replyDate'], '2026-10-19')
            self.assertEqual(merged['interviews'], existing['interviews'])
            self.assertEqual(latest_activity(merged, '2026-11-02'), 'follow-up sent 2026-10-30')

    def test_command_dry_run_then_write_changes_only_followup_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = Path(folder) / 'tracker.html'
            record = {"id": "one", "company": "Example", "role": "Scientist", "jobId": "J-123", "status": "interview", "appliedDate": "2026-10-01", "replyDate": "2026-10-19", "interviews": [{"round": 1, "date": "2026-10-19"}]}
            tracker.write_text('// TRACKER_DATA_START\nconst SAVED_DATA = '+json.dumps([record])+';\n// TRACKER_DATA_END')
            command = [sys.executable, str(Path(__file__).with_name('record_follow_up.py')), '--tracker', str(tracker), '--job-id', 'J-123', '--sent-date', '2026-10-30', '--evidence', 'https://mail.google.com/mail/#sent/1', '--backup-dir', str(Path(folder) / 'backups')]
            original = tracker.read_bytes()
            subprocess.run(command+['--dry-run'], check=True, capture_output=True)
            self.assertEqual(tracker.read_bytes(), original)
            subprocess.run(command, check=True, capture_output=True)
            _, records, _ = read_tracker(tracker)
            actual = records[0]
            self.assertEqual(actual, {**record, 'lastFollowUpDate': '2026-10-30', 'lastFollowUpEvidence': 'https://mail.google.com/mail/#sent/1'})
            self.assertEqual(len(list((Path(folder)/'backups').glob('*.html'))), 1)


if __name__ == '__main__':
    unittest.main()
