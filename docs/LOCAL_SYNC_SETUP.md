# Local ↔ repo sync

Keeps three things in sync between your local working file
(`~/Documents/francisco-job-tracker-2026.html`) and this repo, automatically,
every 30 minutes:

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
runs `tools/sync_local.sh` every 30 minutes:

1. `git pull` the repo
2. `tools/sync_market_history.py` merges `MARKET_HISTORY` between the local file and the repo's copy, writing the union back to both
3. If the repo copy changed, commit and push

Logs: `state/sync_local.log` (the script's own log) and
`state/sync_local.launchd.log` (launchd's stdout/stderr capture).

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
