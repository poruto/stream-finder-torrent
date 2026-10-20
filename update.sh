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

echo "==> Rebuilding and restarting"
docker compose up -d --build

# Drop dangling images left over from previous builds.
docker image prune -f

# By hand (not from cron: building needs sudo): set up local Whisper if it isn't yet.
if ( exec </dev/tty ) 2>/dev/null; then
  # Local Whisper for AI subtitles - on by default, except on 32-bit boards like the
  # Raspberry Pi 3 (too slow; use a Groq key there). Opt out: touch cache/.no-whisper
  enable_local_whisper() {
    [ "$(uname -m)" = "armv7l" ] && return 0
    [ -f cache/.no-whisper ] && return 0
    if [ ! -x "$HOME/whisper.cpp/build/bin/whisper-server" ]; then
      bash install-whisper.sh || { echo "!! Local Whisper could not be installed - a Groq key works too (Settings)."; return 0; }
    fi
    if ! grep -q '^TRANSCRIBE_PROVIDER=' .env; then
      { echo "TRANSCRIBE_PROVIDER=local"; echo "TRANSCRIBE_API_URL=http://127.0.0.1:8178/v1"; } >> .env
      docker compose up -d --force-recreate app  # .env is read when the container starts
    fi
  }
  enable_local_whisper
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://$(hostname).local:5000  (http://${ip:-localhost}:5000)"
