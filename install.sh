#!/usr/bin/env bash
# One-shot installer for Raspberry Pi OS / Debian / Ubuntu:
#   curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/install.sh | bash
# or, inside a clone:  ./install.sh
set -euo pipefail

REPO_URL="https://github.com/poruto/stream-finder-torrent.git"
DIR="${STREAMFINDER_DIR:-$HOME/stream-finder-torrent}"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed. Log out and back in (or reboot), then run this script again."
  exit 0
fi

if [ ! -f docker-compose.yml ]; then
  if [ ! -d "$DIR/.git" ]; then
    echo "==> Cloning into $DIR"
    git clone "$REPO_URL" "$DIR"
  fi
  cd "$DIR"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
fi
echo "Keys and logins can also be entered later on the web: Settings (login admin / admin)."
bash setup-keys.sh .env data/cache/settings.env || true  # asks only for what is still missing

mkdir -p data/torrserver/config data/torrserver/torrents data/cache
echo "==> Building and starting"
docker compose up -d --build

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

echo "==> Enabling automatic updates from GitHub (checked every 5 minutes)"
chmod +x autoupdate.sh update.sh setup-keys.sh
mkdir -p "$HOME/.local/state/streamfinder"
git rev-parse HEAD > "$HOME/.local/state/streamfinder/deployed"   # what is running now
cron_line="*/5 * * * * $PWD/autoupdate.sh"
( crontab -l 2>/dev/null | grep -v "autoupdate.sh"; echo "$cron_line" ) | crontab -

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://${ip:-localhost}:5000"
echo "TorrServer:   http://${ip:-localhost}:8090"
