#!/usr/bin/env bash
# Start (or restart) the Docker containers on the checked-out commit. Uses the
# ready-made image from GitHub when it was already built for this commit (no slow
# build on a Raspberry Pi), else builds it here. Run by install.sh and update.sh.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# The version in the header (the image has no .git), counted from the commits.
STREAMFINDER_COMMIT="$(git rev-parse --short=7 HEAD)"
STREAMFINDER_VERSION="$(python3 streamfinder/version.py 2>/dev/null | cut -d' ' -f1 || true)"
export STREAMFINDER_VERSION="${STREAMFINDER_VERSION:-dev}" STREAMFINDER_COMMIT

image="ghcr.io/poruto/stream-finder-torrent:sha-$STREAMFINDER_COMMIT"
if [ -z "${STREAMFINDER_BUILD:-}" ] && docker pull -q "$image" >/dev/null 2>&1; then
  echo "==> Using the ready-made image $image"
  docker tag "$image" streamfinder:latest
  docker compose up -d --no-build
else
  echo "==> Building the image (a few minutes on a Raspberry Pi)"
  docker compose up -d --build
fi
