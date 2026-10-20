#!/usr/bin/env bash
# Start (or restart) the Docker containers on the checked-out commit. Uses the
# ready-made image from GitHub when it was already built for this commit (no slow
# build on a Raspberry Pi), else builds it here. Run by install.sh and update.sh.
# From autoupdate.sh (STREAMFINDER_WAIT_FOR_IMAGE=1) a fresh commit whose image GitHub
# is still building exits with 75 = "try again on the next run" instead of building.
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
elif [ -n "${STREAMFINDER_WAIT_FOR_IMAGE:-}" ]   && [ $(( $(date +%s) - $(git log -1 --format=%ct) )) -lt 2700 ]; then
  echo "==> The image for $STREAMFINDER_COMMIT is still being built on GitHub - next run"
  exit 75
else
  echo "==> Building the image (a few minutes on a Raspberry Pi)"
  docker compose up -d --build
fi
