# Job Tracker

A self-contained job application tracker, plus the CLI tooling that keeps it updated.

## What's here

- **`francisco-job-tracker-2026.html`** — a single-file HTML/JS app ("Jobs3 · Centro de control 2026"). Open it in a browser to view, filter, and sort applications. All records live embedded in the page itself, so the file is both the app and the database.
- **`tools/update_job_tracker.py`** + **`tools/update_job_tracker`** (wrapper) — CLI that reads a structured posting text file and writes/updates the corresponding record in the tracker HTML.
- **`tools/tracker_context.py`** — read-only script that parses the tracker and dumps the current pipeline (todo/applied/interview/offer/rejected) as JSON, for quick status checks without opening a browser.
- **`docs/STRUCTURED_JOB_POSTING_TEMPLATE.txt`** — the input format `update_job_tracker` expects.
- **`backups/`** — timestamped tracker snapshots written before each update (gitignored; local safety net, not synced).

## Data model

Records live in a JSON array assigned to `SAVED_DATA`, embedded in a `<script>` block between two HTML comment markers:

```html
<!-- TRACKER_DATA_START -->
<script>
const SAVED_DATA = [ { ... }, { ... } ];
</script>
<!-- TRACKER_DATA_END -->
```

The update script finds this block by the markers, parses the array, mutates it in memory, and writes the array back out as the same block — the rest of the HTML/JS app is untouched.

Each record has this shape:

| Field | Meaning |
|---|---|
| `id` | Stable identifier for the record |
| `priority` | e.g. `HIGH` / `MEDIUM` / `LOW` |
| `company`, `companyType` | Employer and its category (e.g. startup, agency) |
| `role`, `location`, `category` | Job title, location, and role category |
| `jobId`, `posted`, `deadline` | Posting metadata |
| `status` | One of `todo`, `applied`, `interview`, `offer`, `rejected`, `withdrawn` |
| `appliedDate`, `replyDate`, `rejectedDate`, `interviewDate` | Pipeline timestamps |
| `requirements`, `responsibilities`, `keywords` | Extracted from the posting |
| `myMatch`, `gaps`, `fitScore` | Self-assessment against the master CV |
| `cvVersion` | Which CV version was submitted |
| `links` | Posting/application URLs |
| `strategicNotes` | Free-text notes on approach/strategy for this application |
| `createdAt` | Record creation timestamp |

## Using `update_job_tracker`

1. Write a posting file following `docs/STRUCTURED_JOB_POSTING_TEMPLATE.txt` (company, role, status, job description sections, etc).
2. Run:

```bash
tools/update_job_tracker path/to/posting.txt --status applied --applied-date 2026-08-03
```

This will:
- back up the current tracker HTML into `backups/` (timestamped, unless `--no-backup`),
- find the existing record for that company/role or create a new one,
- rewrite the `SAVED_DATA` block in place,
- optionally file the posting text under a canonical application folder (`--store-posting`).

Useful flags:
- `--dry-run` — validate and describe the change without writing anything.
- `--print-record` — print the normalized record as JSON.
- `--include-assessment` — also import `FIT_SCORE` / `MY MATCH` / `GAPS` / `NOTES` from the posting file. Off by default because these require a factual review against the master CV rather than being auto-trusted from the posting text.

Paths default to files inside this repo (see `tools/update_job_tracker`), but can be overridden with `JOBS3_TRACKER`, `JOBS3_TRACKER_BACKUP_DIR`, and `JOBS3_APPLICATION_ROOT` environment variables.

## Checking pipeline status

```bash
python3 tools/tracker_context.py francisco-job-tracker-2026.html
```

Dumps counts and records per pipeline stage as JSON — useful for scripting or a quick CLI status check.

## Status

This repo currently holds an independent copy of the tracker, kept in sync manually. The original working copy this was copied from lives locally outside this repo and is still the one being actively edited day-to-day, until the workflow below is validated end-to-end.

## Planned next steps (not yet implemented)

- A scheduled GitHub Actions workflow to run tracker updates in the cloud and commit changes back, so this repo becomes the single source of truth instead of a local file.
- Phone notifications, likely via a Telegram bot — both for push alerts on status changes and two-way commands to check/update status from a phone.

To wire that up we'll need: a Telegram bot token, the target chat ID, and confirmation of how updates should be triggered (on a schedule, via webhook, or both) — as GitHub Actions secrets, never committed to the repo.
