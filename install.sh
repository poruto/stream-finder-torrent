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
  read -r -p "TMDB API key (https://www.themoviedb.org/settings/api): " tmdb_key </dev/tty
  read -r -p "OpenSubtitles API key (optional, Enter to skip): " os_key </dev/tty
  sed -i "s|^TMDB_API_KEY=.*|TMDB_API_KEY=${tmdb_key}|" .env
  sed -i "s|^OPENSUBTITLES_API_KEY=.*|OPENSUBTITLES_API_KEY=${os_key}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
fi

mkdir -p data/torrserver/config data/torrserver/torrents data/cache
echo "==> Building and starting"
docker compose up -d --build

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "StreamFinder: http://${ip:-localhost}:5000"
echo "TorrServer:   http://${ip:-localhost}:8090"
