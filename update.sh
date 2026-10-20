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
  if command -v streamfinder >/dev/null; then
    streamfinder restart
  else
    echo "Services are not installed yet - run: bash termux-install.sh"
  fi
  exit 0
fi

echo "==> Updating TorrServer image"
docker compose pull torrserver

echo "==> Rebuilding and restarting"
docker compose up -d --build

# Drop dangling images left over from previous builds.
docker image prune -f

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://$(hostname).local:5000  (http://${ip:-localhost}:5000)"
