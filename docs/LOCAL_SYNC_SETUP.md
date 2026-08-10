# Local ↔ repo sync

Keeps three things in sync between your local working file
(`~/Documents/francisco-job-tracker-2026.html`) and this repo, automatically,
every 12 hours:

- `MARKET_HISTORY` (discovered job postings) — merged both directions.
- `DISCARDED_POSTINGS` (things you clicked "No postularé" on in the app's
  Nuevas page) — merged both directions, so a discard made locally reaches
  the cloud discovery pipeline and stops it from re-alerting on the same
  posting, and vice versa.
- `state/tracked_identities.json` — a one-way, read-only export of your
  local `SAVED_DATA`'s company+role identity keys (never the data itself),
  so the cloud side recognizes "already applied to this" even when the
  status update only ever happened on this machine.

## Why not `SAVED_DATA` itself

`SAVED_DATA` (your tracked applications and their status) is **not** synced
directly. WhatsApp two-way commands (`update: <company> | <status>`) write
directly to the repo's `SAVED_DATA`, and a blind local↔repo merge of that
field could overwrite those changes with stale local data. `MARKET_HISTORY`
and `DISCARDED_POSTINGS` are metadata with no such conflict, so merging them
both ways is safe — entries are matched by company + job ID (or company +
title/role as a fallback), and duplicates are automatically deduplicated.
For "already applied" recognition without the clobber risk, only a derived,
additive-only set of identity keys is exported (`tracked_identities.json`),
never the actual application records.

## How it runs

A macOS LaunchAgent (`~/Library/LaunchAgents/com.francisco.jobtracker-sync.plist`)
runs `tools/sync_local.sh` every hour while the Mac is running, with an
additional catch up run when the agent loads. Real changes are pushed
immediately; quiet runs publish a health heartbeat at most every four hours:

1. `git pull` the repo
2. `tools/sync_market_history.py` merges `MARKET_HISTORY` between the local file and the repo's copy, writing the union back to both
3. Stamps a `LAST_LOCAL_SYNC_AT` heartbeat timestamp into both copies every four hours at most when no data changed. A sync that ran and found nothing new is different information from a sync that never ran at all, and the periodic heartbeat preserves that distinction without creating a commit every hour.
4. Commits and pushes -- `"Local sync: MARKET_HISTORY/DISCARDED_POSTINGS updated from local tracker"` if there was a real data change, `"Local sync: heartbeat, no data changes"` otherwise.

Logs: `state/sync_local.log` (the script's own log) and
`state/sync_local.launchd.log` (launchd's stdout/stderr capture).

### Freshness signals fed by this

The same "Local sync: ..." commit is the single source of truth for three
separate freshness indicators, deliberately kept in sync rather than each
inventing its own notion of "recent":
- The tracker UI's colored dot next to the app count in the header (green <13h, amber 13-30h, red 30h+).
- The `Última sync local: ...` line in the WhatsApp digest and high-fit alert (`last_local_sync_line()` in `tools/discover_postings.py`).
- `.github/workflows/sync-watchdog.yml`, which runs once a day and pages Francisco over WhatsApp if no `Local sync:` commit landed in the last 24h.

### Failure notification

Two different failure classes, two different mechanisms -- neither alone is
enough:
- **The script runs but fails** (e.g. `git push` exhausts its 3 retries): `sync_local.sh` itself calls `gh workflow run whatsapp.yml -f raw_message=...` directly, using the `gh` CLI's already-authenticated local session (the same one `git push` relies on) -- no separate WhatsApp credentials are stored on this machine.
- **The script never starts at all**: nothing inside the script can detect or report this, by definition. This is exactly what happened 2026-08-05 through 2026-08-09 -- macOS silently blocked the LaunchAgent's `bash` process from opening a script under `~/Documents` (Full Disk Access / TCC protection; system-wide FDA for `/bin/bash` is the fix, added under System Settings -> Privacy & Security -> Full Disk Access), and `bash` itself failed with `Operation not permitted` on every single scheduled attempt, 5 days straight, with nothing in `state/sync_local.log` to show for it since the script never got far enough to write to it. Only `sync-watchdog.yml`, checking from the cloud side for the *absence* of a recent commit, can catch this class of failure.

## Managing the job

```bash
# Check it's running
launchctl list | grep jobtracker

# Stop it
launchctl unload ~/Library/LaunchAgents/com.francisco.jobtracker-sync.plist

# Start it again
launchctl load ~/Library/LaunchAgents/com.francisco.jobtracker-sync.plist

# Run a sync manually, right now
bash tools/sync_local.sh
```

## Requirements

Needs `git push` to work non-interactively from this machine. That's
already set up via `gh auth setup-git` (uses the `gh` CLI's stored
credentials as a git credential helper) — no separate token needed here.
