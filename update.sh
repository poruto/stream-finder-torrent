#!/usr/bin/env bash
# Pull the latest StreamFinder from GitHub and redeploy.
#   ssh poruto@raspberrypi.local ~/stream-finder-torrent/update.sh
# .env, data/ and cache/ are gitignored, so the hard reset keeps config and state.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

echo "==> Fetching origin/master"
git fetch origin
git reset --hard origin/master

# Android (Termux) runs without Docker: reinstall dependencies and restart the services.
if [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *com.termux* ]]; then
  echo "==> Installing Python dependencies"
  pip install -q -r requirements.txt
  bash setup-keys.sh .env || true
  if command -v streamfinder >/dev/null; then
    streamfinder restart
  else
    echo "Services are not installed yet - run: bash termux-install.sh"
  fi
  exit 0
fi

echo "==> Updating TorrServer image"
docker compose pull torrserver

# Run by hand: ask for keys that are still missing (cron has no terminal, skips this).
bash setup-keys.sh .env data/cache/settings.env || true

echo "==> Starting"
bash docker-up.sh

# Drop dangling images left over from previous builds.
docker image prune -f

# By hand (not from cron: building needs sudo): set up local Whisper if it isn't yet.
if ( exec </dev/tty ) 2>/dev/null; then
  bash install-whisper.sh --enable  # not on a Raspberry Pi 3; opt out: touch cache/.no-whisper
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://$(hostname).local:5000  (http://${ip:-localhost}:5000)"
