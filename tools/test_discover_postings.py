#!/usr/bin/env python3

import json
import unittest
from unittest.mock import patch

import discover_postings


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class DiscoverPostingsTests(unittest.TestCase):
    def test_medical_scientific_liaison_is_high_fit(self):
        score = discover_postings.compute_fit("Medical Scientific Liaison Vaccines")
        self.assertGreaterEqual(score, discover_postings.HIGH_FIT_THRESHOLD)

    @patch("discover_postings.urllib.request.urlopen")
    def test_workday_fetches_later_pages(self, mock_urlopen):
        first_page = [
            {
                "title": f"Unrelated role {index}",
                "locationsText": "United States",
                "externalPath": f"/job/Unrelated_{index}",
            }
            for index in range(20)
        ]
        target = {
            "title": "Medical Scientific Liaison Vaccines",
            "locationsText": "Chile - Santiago",
            "externalPath": "/job/Medical-Scientific-Liaison-Vaccines_4955162-1",
        }

        def response_for(request, timeout):
            body = json.loads(request.data.decode())
            if body["offset"] == 0:
                return FakeResponse({"total": 21, "jobPostings": first_page})
            if body["offset"] == 20:
                return FakeResponse({"total": 21, "jobPostings": [target]})
            return FakeResponse({"total": 21, "jobPostings": []})

        mock_urlopen.side_effect = response_for
        source = {
            "company": "Pfizer",
            "tenant": "pfizer",
            "wd": "wd1",
            "site": "PfizerCareers",
        }

        entries = discover_postings.fetch_workday(source, "Medical Scientific Liaison")

        self.assertEqual(2, mock_urlopen.call_count)
        self.assertEqual(1, len(entries))
        self.assertEqual("Medical Scientific Liaison Vaccines", entries[0]["title"])
        self.assertIn("4955162-1", entries[0]["jobId"])


if __name__ == "__main__":
    unittest.main()
