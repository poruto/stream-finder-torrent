#!/usr/bin/env bash
# Ask for the API keys and logins that are not set yet, and save them into .env.
#   bash setup-keys.sh [.env] [settings file written by the web Settings page]
# Run by the installers and by a manual update; without a terminal (cron, the
# Termux update service) it does nothing. Whatever is skipped once (Enter) is not
# asked again - set it later on the web: Settings (admin login).
# Exit status 2 = something was saved (restart the app to use it).
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"
ENV_FILE="${1:-.env}"
SETTINGS_FILE="${2:-}"
ASKED_FILE="$(dirname "$ENV_FILE")/cache/.setup-asked"

# Only when someone can answer (SETUP_KEYS_TTY: answers from a file, for tests).
TTY="${SETUP_KEYS_TTY:-/dev/tty}"
( exec <"$TTY" ) 2>/dev/null || exit 0
exec 3<"$TTY"

mkdir -p "$(dirname "$ASKED_FILE")"
touch "$ENV_FILE" "$ASKED_FILE"

value_of() {  # current value in .env or the web settings file
  local key="$1" file line
  for file in "$SETTINGS_FILE" "$ENV_FILE"; do
    [ -n "$file" ] && [ -f "$file" ] || continue
    line=$(grep -E "^${key}=" "$file" | tail -n 1)
    line="${line#*=}"; line="${line#\'}"; line="${line%\'}"
    [ -n "$line" ] && { printf '%s' "$line"; return; }
  done
}

saved=0
save() {  # KEY VALUE -> .env (single-quoted: passwords may contain * $ #)
  local key="$1" value="${2//\'/}" tmp
  saved=1
  tmp="$(mktemp)"
  grep -vE "^${key}=" "$ENV_FILE" > "$tmp" || true
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  cat "$tmp" > "$ENV_FILE" && rm -f "$tmp"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

needs() {  # GROUP: not set and not skipped before
  local group="$1"; shift
  grep -qx "$group" "$ASKED_FILE" && return 1
  local key
  for key in "$@"; do [ -n "$(value_of "$key")" ] || return 0; done
  return 1
}

asked() { echo "$1" >> "$ASKED_FILE"; }

ask() {  # VAR PROMPT [secret]
  local reply
  if [ "${3:-}" = secret ]; then
    read -r -s -p "$2" reply <&3; echo >&2
  else
    read -r -p "$2" reply <&3
  fi
  printf -v "$1" '%s' "$reply"
}

changed=0
intro() {
  [ "$changed" = 0 ] && echo "==> Settings that are not filled in yet (Enter = skip, can be set later on the web)"
  changed=1
}

if needs tmdb TMDB_API_KEY; then
  intro
  ask v "TMDB API key (required for the catalogue, https://www.themoviedb.org/settings/api): "
  [ -n "$v" ] && save TMDB_API_KEY "$v"
  asked tmdb
fi

if needs opensubtitles OPENSUBTITLES_API_KEY; then
  intro
  ask v "OpenSubtitles API key (optional, https://www.opensubtitles.com/consumers): "
  [ -n "$v" ] && save OPENSUBTITLES_API_KEY "$v"
  asked opensubtitles
fi

if needs sktorrent SKTORRENT_USERNAME SKTORRENT_PASSWORD; then
  intro
  echo "SkTorrent: Czech/Slovak films and series with dubbing (free account at https://sktorrent.eu)."
  ask user "  SkTorrent username (Enter = skip): "
  if [ -n "$user" ]; then
    ask pass "  SkTorrent password: " secret
    save SKTORRENT_USERNAME "$user"
    save SKTORRENT_PASSWORD "$pass"
  fi
  asked sktorrent
fi

# Local Whisper is set up by the installers; a Groq key is the optional, faster alternative.
if ! grep -qx transcribe "$ASKED_FILE" && [ -z "$(value_of TRANSCRIBE_API_KEY)" ] && [ -z "$(value_of TRANSCRIBE_API_URL)" ]   && [ ! -x "$HOME/whisper.cpp/build/bin/whisper-server" ]; then
  intro
  echo "AI subtitles from the audio: free Groq key from https://console.groq.com/keys (no card needed)."
  ask v "  Groq API key (Enter = skip): " secret
  if [ -n "$v" ]; then
    save TRANSCRIBE_API_KEY "$v"
    save TRANSCRIBE_PROVIDER api
    save TRANSCRIBE_ENABLED true
  fi
  asked transcribe
fi

if [ "$changed" = 1 ] && [ "$saved" = 1 ]; then
  echo "Saved to $ENV_FILE."
  exit 2
fi
exit 0
