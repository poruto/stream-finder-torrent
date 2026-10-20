#!/usr/bin/env bash
# Redeploy when GitHub master has a new commit. Run from cron (install.sh adds it):
#   */5 * * * * /path/to/stream-finder-torrent/autoupdate.sh
# Log: ~/.local/state/streamfinder/autoupdate.log
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
STATE="$HOME/.local/state/streamfinder"
LOG="$STATE/autoupdate.log"
mkdir -p "$STATE"

# Never run two updates at once (an ARM build takes a while).
exec 9>"$STATE/lock"
if command -v flock >/dev/null; then flock -n 9 || exit 0; fi

cd "$REPO"
git fetch -q origin master
remote=$(git rev-parse origin/master)
deployed=$(cat "$STATE/deployed" 2>/dev/null || git rev-parse HEAD)

[ "$remote" = "$deployed" ] && exit 0
# A commit that already failed is retried only after a new push.
[ "$remote" = "$(cat "$STATE/failed" 2>/dev/null || true)" ] && exit 0

# Keep the log small.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "=== $(date '+%F %T') updating ${deployed:0:7} -> ${remote:0:7}" >> "$LOG"
if nice -n 10 bash "$REPO/update.sh" >> "$LOG" 2>&1; then
  echo "$remote" > "$STATE/deployed"
  rm -f "$STATE/failed"
  echo "=== $(date '+%F %T') OK" >> "$LOG"
else
  echo "$remote" > "$STATE/failed"
  echo "=== $(date '+%F %T') FAILED (see above)" >> "$LOG"
fi
