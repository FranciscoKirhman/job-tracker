#!/usr/bin/env python3
"""Record a verified sent follow-up without modifying application state."""

import argparse
from pathlib import Path

from follow_up import follow_up_fields, validate_follow_up_order
from update_job_tracker import backup_tracker, read_tracker, stable_job_id, updated_html, write_atomic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracker", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--sent-date", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    fields = follow_up_fields(args.sent_date, args.evidence)
    html, jobs, match = read_tracker(args.tracker)
    key = stable_job_id(args.job_id)
    matches = [job for job in jobs if key and stable_job_id(job.get("jobId", "")) == key]
    if len(matches) != 1:
        parser.error("The exact job ID must match one existing application.")
    job = matches[0]
    validate_follow_up_order(job, fields)
    before = dict(job)
    job.update(fields)
    print(f"{job['company']}: sent follow-up {fields['lastFollowUpDate']}; state remains {job['status']}")
    if args.dry_run:
        print("Dry run: no files changed")
        return
    backup_tracker(args.tracker, args.backup_dir)
    write_atomic(args.tracker, updated_html(html, match, jobs))
    _, saved, _ = read_tracker(args.tracker)
    actual = next(item for item in saved if item['id'] == job['id'])
    assert actual == {**before, **fields}, "Follow-up verification failed"
    print("Verified date, evidence and preservation of application fields. The widget refreshes on the atomic write.")


if __name__ == "__main__":
    main()
