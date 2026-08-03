#!/usr/bin/env python3
"""Regression tests for the read only tracker context exporter."""

from __future__ import annotations

import unittest
from pathlib import Path

import tracker_context


TRACKER = Path("/Users/franciscokirhman/Documents/francisco-job-tracker-2026.html")


class TrackerContextTests(unittest.TestCase):
    def test_normalize_key_removes_accents_and_punctuation(self) -> None:
        self.assertEqual(tracker_context.normalize_key("Oncología – LATAM"), "oncologialatam")

    def test_placeholder_job_id_is_not_stable(self) -> None:
        self.assertEqual(tracker_context.stable_job_id("LinkedIn"), "")
        self.assertEqual(tracker_context.stable_job_id("unknown"), "")

    def test_real_job_id_is_stable(self) -> None:
        self.assertEqual(tracker_context.stable_job_id("JR-150239"), "jr150239")

    def test_placeholder_ids_fall_back_to_company_and_role(self) -> None:
        first = {"company": "Alpha", "role": "Role A", "jobId": "LinkedIn"}
        second = {"company": "Beta", "role": "Role B", "jobId": "LinkedIn"}
        self.assertNotEqual(
            tracker_context.duplicate_key(first), tracker_context.duplicate_key(second)
        )

    def test_same_real_job_id_matches_changed_role(self) -> None:
        first = {"company": "Alpha", "role": "Old title", "jobId": "JR-150239"}
        second = {"company": "Alpha", "role": "New title", "jobId": "JR150239"}
        self.assertEqual(
            tracker_context.duplicate_key(first), tracker_context.duplicate_key(second)
        )

    def test_company_and_role_match_when_only_one_record_has_job_id(self) -> None:
        first = {"company": "Alpha", "role": "Role A", "jobId": "JR150239"}
        second = {"company": "Álpha", "role": "Role A", "jobId": ""}
        self.assertTrue(tracker_context.same_identity(first, second))
        self.assertEqual(len(tracker_context.duplicate_groups([first, second])), 1)

    def test_different_real_job_ids_are_distinct_requisitions(self) -> None:
        first = {"company": "Alpha", "role": "Role A", "jobId": "JR100"}
        second = {"company": "Alpha", "role": "Role A", "jobId": "JR200"}
        self.assertFalse(tracker_context.same_identity(first, second))
        self.assertEqual(tracker_context.duplicate_groups([first, second]), [])

    def test_rejected_record_with_interview_reached_interview(self) -> None:
        record = {
            "status": "rejected",
            "interviews": [{"round": 1, "date": "2026-06-10"}],
        }
        self.assertTrue(tracker_context.reached_interview(record))
        self.assertEqual(tracker_context.derived_stage(record), "rejected")

    def test_current_tracker_live_invariants(self) -> None:
        jobs = tracker_context.read_tracker(TRACKER)
        context = tracker_context.build_context(TRACKER, jobs)
        self.assertEqual(context["recordCount"], len(jobs))
        self.assertGreater(context["recordCount"], 0)
        self.assertEqual(context["duplicates"], [])
        allowed = {"todo", "applied", "interview", "offer", "rejected", "withdrawn"}
        self.assertTrue(all(record.get("status") in allowed for record in context["records"]))

    def test_tracker_uses_local_assets_and_santiago_date(self) -> None:
        html = TRACKER.read_text(encoding="utf-8")
        self.assertIn('src="tracker-assets/chart.umd.js"', html)
        self.assertNotIn("cdnjs.cloudflare.com", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertIn("timeZone:'America/Santiago'", html)
        self.assertIn("Content-Security-Policy", html)

    def test_machine_context_excludes_assessment_fields(self) -> None:
        jobs = tracker_context.read_tracker(TRACKER)
        context = tracker_context.build_context(TRACKER, jobs)
        for record in context["records"]:
            self.assertNotIn("myMatch", record)
            self.assertNotIn("gaps", record)
            self.assertNotIn("fitScore", record)
            self.assertNotIn("strategicNotes", record)


if __name__ == "__main__":
    unittest.main()
