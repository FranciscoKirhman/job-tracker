# Job Tracker

A self-contained job application tracker, plus the CLI tooling that keeps it updated.

## What's here

- **`francisco-job-tracker-2026.html`** — a single-file HTML/JS app ("Jobs3 · Centro de control 2026"). Open it in a browser to view, filter, and sort applications. All records live embedded in the page itself, so the file is both the app and the database.
- **`tools/update_job_tracker.py`** + **`tools/update_job_tracker`** (wrapper) — CLI that reads a structured posting text file and writes/updates the corresponding record in the tracker HTML.
- **`tools/tracker_context.py`** — read-only script that parses the tracker and dumps the current pipeline (todo/applied/interview/offer/rejected) as JSON, for quick status checks without opening a browser.
- **`docs/STRUCTURED_JOB_POSTING_TEMPLATE.txt`** — the input format `update_job_tracker` expects.
- **`backups/`** — timestamped tracker snapshots written before each update (gitignored; local safety net, not synced).
- **`tools/whatsapp_command.py`** + **`tools/send_whatsapp.py`** — parse a short WhatsApp message (`pipeline`, `update: <company> | <status>`) and send replies via the Meta Cloud API.
- **`.github/workflows/whatsapp.yml`** — scheduled + on-demand GitHub Actions workflow that runs the above and commits tracker changes back.
- **`tools/check_openai_usage_resets.py`** + **`.github/workflows/openai-usage-reset-watch.yml`** — hourly read-only check of official OpenAI Developer Community sources; newly identified ChatGPT Work/Codex reset announcements use the existing WhatsApp secrets and sender path.
- **`cloudflare-worker/`** — always-on relay that receives your WhatsApp replies (GitHub Actions can't host a webhook) and forwards them to the workflow. See `docs/WHATSAPP_SETUP.md`.
- **`tools/mobile_sync.py`** + **`.github/workflows/mobile-sync.yml`** — the same Cloudflare Worker also bridges phone swipes (discard/save from the "Nuevas" page, and their undo) into the repo, since the static app has no backend of its own to write to. See `docs/CLOUDFLARE_SYNC_SETUP.md`.
- **`tools/discover_postings.py`** + **`tools/linkedin-search/`** — finds new job postings (LinkedIn + Workday, no LLM calls) and adds them to `MARKET_HISTORY`, alerting immediately over WhatsApp for high-fit matches. See `docs/JOB_DISCOVERY.md`.
- **`tools/sync_market_history.py`** + **`tools/sync_local.sh`** — keeps `MARKET_HISTORY` in sync between this repo and the local working file, on a macOS LaunchAgent schedule. See `docs/LOCAL_SYNC_SETUP.md`.

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

## WhatsApp notifications and two-way commands

Set up: [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md). Once configured, a scheduled GitHub Actions workflow sends a daily pipeline digest to WhatsApp, and you can text back commands (`pipeline`, `update: <company> | <status>`) that update the tracker and commit the change — no PC required.

## Mobile sync (swipe from the phone)

Set up: [docs/CLOUDFLARE_SYNC_SETUP.md](docs/CLOUDFLARE_SYNC_SETUP.md). Swiping right/left on the "Nuevas" page (and deleting a job from Procesos, or undoing any of those) pushes the change straight to this repo through the same Worker used for WhatsApp — no manual save-and-push step, no waiting for the local sync LaunchAgent.

## Status

The local working copy and this repo sync automatically: a macOS LaunchAgent runs `tools/sync_local.sh` every 30 minutes (see [docs/LOCAL_SYNC_SETUP.md](docs/LOCAL_SYNC_SETUP.md)) to merge discovered postings, discards, and tracked-application identities both ways, and WhatsApp two-way commands write status changes directly to this repo via GitHub Actions. Mobile-sync swipes (above) also write directly to this repo, immediately. The one thing that's still local-authoritative rather than auto-synced is `SAVED_DATA` edits made *manually in a browser session* (adding/editing an application by hand, not via a swipe) — those need an explicit "Guardar" in the app plus a `git push`, since a static page can't silently overwrite an arbitrary file on disk without you picking it via a save dialog. See `docs/LOCAL_SYNC_SETUP.md` for the LaunchAgent side of that story.
