#!/usr/bin/env bash
# Publish the canonical application snapshot and synchronize discovery metadata
# between the local live tracker and this repo.
# Run by a launchd job (see docs/LOCAL_SYNC_SETUP.md) so new postings found
# either locally or by cloud automation reach both copies without manual steps.
set -euo pipefail

REPO_DIR="/Users/franciscokirhman/Documents/Career/job-tracker-repo"
LOCAL_TRACKER="/Users/franciscokirhman/Documents/francisco-job-tracker-2026.html"
LOG_FILE="$REPO_DIR/state/sync_local.log"
REPORTS_DIR="/Users/franciscokirhman/Documents/Codex/2026-07-24/s/outputs/chile_careers_reports"
MONITOR_SUMMARY="$REPO_DIR/state/chile_monitor_summary.json"
HEARTBEAT_INTERVAL_SECONDS=14400

mkdir -p "$REPO_DIR/state"
exec >> "$LOG_FILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

cd "$REPO_DIR"

# This directory doubles as an interactive working copy (editor sessions,
# manual edits, an in-progress git operation) -- never force-discard
# anything here the way an ephemeral CI checkout safely could. If there's
# already uncommitted state sitting in the tree when this fires, skip the
# cycle entirely rather than risk mixing it into an automated commit or
# clobbering it; the next scheduled run picks up cleanly once
# it's gone.
if [ -n "$(git status --porcelain)" ]; then
  echo "Uncommitted changes present in $REPO_DIR -- skipping this cycle."
  exit 0
fi

git pull --quiet origin main

# Export the newest append-only inventory and recovery evidence for the
# cloud-hosted 08:00 WhatsApp digest. This records attempt counts and coverage
# without copying or mutating the source reports.
python3 tools/build_monitor_summary.py \
  --reports-dir "$REPORTS_DIR" \
  --output "$MONITOR_SUMMARY" \
  --tracker "$LOCAL_TRACKER" \
  --tracker "$REPO_DIR/francisco-job-tracker-2026.html"

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
last_heartbeat_epoch=$(git log -1 --format=%ct --grep='^Local sync:' || true)
now_epoch=$(date +%s)
heartbeat_args=()
if [ -z "$last_heartbeat_epoch" ] || [ $((now_epoch - last_heartbeat_epoch)) -ge "$HEARTBEAT_INTERVAL_SECONDS" ]; then
  heartbeat_args=(--heartbeat)
fi

for attempt in 1 2 3; do
  canonical_output=$(python3 tools/publish_canonical_snapshot.py \
    --canonical "$LOCAL_TRACKER" \
    --repo "$REPO_DIR/francisco-job-tracker-2026.html")
  echo "$canonical_output"

  if [ "${#heartbeat_args[@]}" -gt 0 ]; then
    sync_output=$(python3 tools/sync_market_history.py --local "$LOCAL_TRACKER" --repo "$REPO_DIR/francisco-job-tracker-2026.html" --heartbeat)
  else
    sync_output=$(python3 tools/sync_market_history.py --local "$LOCAL_TRACKER" --repo "$REPO_DIR/francisco-job-tracker-2026.html")
  fi
  echo "$sync_output"

  # Actual data changes are pushed immediately. Quiet cycles stamp and push a
  # health heartbeat at most every four hours, which still exposes a silent
  # launch failure without creating a commit every hour.
  if git diff --quiet -- francisco-job-tracker-2026.html state/tracked_identities.json state/chile_monitor_summary.json 2>/dev/null; then
    echo "No repo changes and no heartbeat is due."
    exit 0
  fi

  if echo "$canonical_output" | grep -q "^CANONICAL_CHANGED=1"; then
    commit_msg="Local sync: publish canonical application snapshot"
  elif echo "$sync_output" | grep -q "^REPO_CHANGED=1"; then
    commit_msg="Local sync: MARKET_HISTORY/DISCARDED_POSTINGS updated from local tracker"
  else
    commit_msg="Local sync: heartbeat, no data changes"
  fi

  git add francisco-job-tracker-2026.html state/tracked_identities.json state/chile_monitor_summary.json
  git commit --quiet -m "$commit_msg"
  if git push --quiet origin main; then
    echo "Pushed on attempt $attempt: $commit_msg"
    exit 0
  fi

  echo "Push rejected on attempt $attempt/3 -- another writer (likely mobile-sync) landed first. Retrying from a fresh pull..."
  git reset --quiet HEAD~1
  git checkout --quiet -- francisco-job-tracker-2026.html state/tracked_identities.json state/chile_monitor_summary.json
  git pull --quiet origin main
  python3 tools/build_monitor_summary.py \
    --reports-dir "$REPORTS_DIR" \
    --output "$MONITOR_SUMMARY" \
    --tracker "$LOCAL_TRACKER" \
    --tracker "$REPO_DIR/francisco-job-tracker-2026.html"
done

echo "All push attempts failed after 3 tries -- giving up this cycle, will retry on the next hourly run."
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
