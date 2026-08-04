# Local ↔ repo sync

Keeps `MARKET_HISTORY` (discovered job postings) in sync between your local
working file (`~/Documents/francisco-job-tracker-2026.html`) and this repo,
in both directions — automatically, every 30 minutes.

## Why only `MARKET_HISTORY`

`SAVED_DATA` (your tracked applications and their status) is **not** touched
by this sync. WhatsApp two-way commands (`update: <company> | <status>`)
write directly to the repo's `SAVED_DATA`, and a blind local↔repo merge of
that field could overwrite those changes with stale local data. `MARKET_HISTORY`
is just discovered-posting metadata (company, title, url, timestamps) with
no such conflict, so merging it both ways is safe — entries are matched by
company + job ID (or company + title + url as a fallback), and duplicates
are automatically deduplicated.

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
