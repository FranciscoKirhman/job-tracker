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
git pull --quiet origin main

python3 tools/sync_market_history.py --local "$LOCAL_TRACKER" --repo "$REPO_DIR/francisco-job-tracker-2026.html"

if ! git diff --quiet -- francisco-job-tracker-2026.html state/tracked_identities.json 2>/dev/null; then
  git add francisco-job-tracker-2026.html state/tracked_identities.json
  git commit -m "Sync MARKET_HISTORY + tracked identities from local tracker"
  git push --quiet origin main
  echo "Pushed update."
else
  echo "No repo changes."
fi
