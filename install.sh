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
  echo "API keys can also be entered later on the web: Settings (login admin / admin)."
  read -r -p "TMDB API key (Enter = later on the web): " tmdb_key </dev/tty || tmdb_key=""
  read -r -p "OpenSubtitles API key (optional, Enter to skip): " os_key </dev/tty || os_key=""
  sed -i "s|^TMDB_API_KEY=.*|TMDB_API_KEY=${tmdb_key}|" .env
  sed -i "s|^OPENSUBTITLES_API_KEY=.*|OPENSUBTITLES_API_KEY=${os_key}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
fi

mkdir -p data/torrserver/config data/torrserver/torrents data/cache
echo "==> Building and starting"
docker compose up -d --build

echo "==> Enabling automatic updates from GitHub (checked every 5 minutes)"
chmod +x autoupdate.sh update.sh
mkdir -p "$HOME/.local/state/streamfinder"
git rev-parse HEAD > "$HOME/.local/state/streamfinder/deployed"   # what is running now
cron_line="*/5 * * * * $PWD/autoupdate.sh"
( crontab -l 2>/dev/null | grep -v "autoupdate.sh"; echo "$cron_line" ) | crontab -

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://${ip:-localhost}:5000"
echo "TorrServer:   http://${ip:-localhost}:8090"
