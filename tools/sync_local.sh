#!/usr/bin/env bash
# Periodic MARKET_HISTORY sync between the local live tracker and this repo.
# Run by a launchd job (see docs/LOCAL_SYNC_SETUP.md) so new postings found
# either locally or by cloud automation reach both copies without manual steps.
set -euo pipefail

REPO_DIR="/Users/franciscokirhman/Documents/Career/job-tracker-repo"
LOCAL_TRACKER="/Users/franciscokirhman/Documents/francisco-job-tracker-2026.html"
LOG_FILE="$REPO_DIR/state/sync_local.log"

mkdir -p "$REPO_DIR/state"
exec >> "$LOG_FILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

cd "$REPO_DIR"

# This directory doubles as an interactive working copy (editor sessions,
# manual edits, an in-progress git operation) -- never force-discard
# anything here the way an ephemeral CI checkout safely could. If there's
# already uncommitted state sitting in the tree when this fires, skip the
# cycle entirely rather than risk mixing it into an automated commit or
# clobbering it; the next scheduled run (12h later) picks up cleanly once
# it's gone.
if [ -n "$(git status --porcelain)" ]; then
  echo "Uncommitted changes present in $REPO_DIR -- skipping this cycle."
  exit 0
fi

git pull --quiet origin main

# mobile-sync.yml (GitHub Actions) can also be pushing to this same repo at
# any moment -- rapid phone swipes fire it repeatedly. A single git push
# here would fail outright if one lands between this run's pull and push.
# Retry by discarding just the two files this script manages (never a
# broader reset) and re-running the merge fresh against the latest pull --
# sync_market_history.py's union-merge is idempotent, so recomputing it
# after a fresh pull converges correctly instead of needing a rebase (which
# would likely conflict anyway: both writers regex-replace the same JSON
# blocks whole, so two commits touching them rarely rebase cleanly even
# when the underlying edits don't actually clash -- see mobile-sync.yml's
# own comments for the same lesson learned there).
for attempt in 1 2 3; do
  sync_output=$(python3 tools/sync_market_history.py --local "$LOCAL_TRACKER" --repo "$REPO_DIR/francisco-job-tracker-2026.html")
  echo "$sync_output"

  # sync_market_history.py now stamps LAST_LOCAL_SYNC_AT into
  # francisco-job-tracker-2026.html on every run, including no-op ones, so
  # this diff is essentially never empty anymore -- that's intentional: a
  # sync that ran and found nothing new should still push a heartbeat,
  # otherwise a silent failure (script never even starting -- see the
  # 2026-08-05 Full Disk Access incident, docs/LOCAL_SYNC_SETUP.md) is
  # indistinguishable from a quiet week with no new postings.
  if git diff --quiet -- francisco-job-tracker-2026.html state/tracked_identities.json 2>/dev/null; then
    echo "No repo changes (unexpected now that every run stamps a heartbeat -- investigate if this recurs)."
    exit 0
  fi

  if echo "$sync_output" | grep -q "^REPO_CHANGED=1"; then
    commit_msg="Local sync: MARKET_HISTORY/DISCARDED_POSTINGS updated from local tracker"
  else
    commit_msg="Local sync: heartbeat, no data changes"
  fi

  git add francisco-job-tracker-2026.html state/tracked_identities.json
  git commit --quiet -m "$commit_msg"
  if git push --quiet origin main; then
    echo "Pushed on attempt $attempt: $commit_msg"
    exit 0
  fi

  echo "Push rejected on attempt $attempt/3 -- another writer (likely mobile-sync) landed first. Retrying from a fresh pull..."
  git reset --quiet HEAD~1
  git checkout --quiet -- francisco-job-tracker-2026.html state/tracked_identities.json
  git pull --quiet origin main
done

echo "All push attempts failed after 3 tries -- giving up this cycle, will retry in 12 hours."
# Best-effort immediate alert for THIS failure class (the script ran, but
# git push exhausted its retries) -- a different, narrower thing than the
# fully-silent "bash itself never started" failure mode (2026-08-05's TCC/
# Full Disk Access block), which can't reach this point at all since the
# script never runs; that class is instead caught from the cloud side by
# .github/workflows/sync-watchdog.yml checking for a stale heartbeat.
# `gh` already carries the 'workflow' scope locally (same auth used for git
# push), so this reuses the existing whatsapp.yml path rather than needing
# separate WhatsApp credentials stored on this machine.
gh workflow run whatsapp.yml --repo FranciscoKirhman/job-tracker \
  -f raw_message="⚠ Sync local (Mac) falló tras 3 intentos de push. Ver state/sync_local.log." \
  || echo "Could not send failure alert via gh (gh workflow run itself failed)."
exit 1
