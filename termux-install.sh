#!/data/data/com.termux/files/usr/bin/bash
# StreamFinder on an Android phone (Termux), one command:
#   curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/termux-install.sh | bash
#
# Installs Python, git, ffmpeg (audio conversion), the app and TorrServer and runs both
# as always-on services (termux-services / runit: restarted if they crash, started again
# after a reboot with Termux:Boot). A third service installs new versions from GitHub
# every 30 minutes. Control them with:
#   streamfinder start | stop | restart | status | update | logs
set -euo pipefail

REPO_URL="https://github.com/poruto/stream-finder-torrent.git"
DIR="$HOME/stream-finder-torrent"
TS_TAG="${TORRSERVER_TAG:-MatriX.147}"
SV="$PREFIX/var/service"

# --refresh (used by `streamfinder update`): only rewrite the services and the command.
REFRESH=0
[ "${1:-}" = "--refresh" ] && REFRESH=1

if [ "$REFRESH" = 0 ]; then
echo "==> Installing packages"
# --force-confnew: never stop and ask about changed config files (stdin is the curl pipe).
APT_OPTS=(-y -o Dpkg::Options::=--force-confnew)
# A half-configured ffmpeg from an earlier failed run blocks every apt command; drop it.
if ! dpkg --configure -a; then
  dpkg --remove --force-remove-reinstreq ffmpeg || true
fi
# A full upgrade first: new packages (ffmpeg) need current libraries, a partly
# updated Termux fails with "library ... not found" errors.
pkg upgrade "${APT_OPTS[@]}"
pkg install "${APT_OPTS[@]}" python git curl termux-services procps
# ffmpeg (incl. ffprobe) converts AC3/DTS audio; without it everything else still works.
if ! pkg install "${APT_OPTS[@]}" ffmpeg || ! ffmpeg -version >/dev/null 2>&1; then
  dpkg --remove --force-remove-reinstreq ffmpeg >/dev/null 2>&1 || true  # don't leave apt broken
  echo "!! ffmpeg could not be installed - audio conversion and subtitle sync are disabled."
  echo "!! Fix later with: pkg upgrade && pkg install ffmpeg && streamfinder restart"
fi

if [ -d "$DIR/.git" ]; then
  # fetch + reset (not pull): works for the shallow clone even after a force-push
  git -C "$DIR" fetch --depth 1 origin master
  git -C "$DIR" reset --hard origin/master
else
  git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR"

echo "==> Installing Python dependencies"
# No "pip install --upgrade pip": Termux manages pip itself and refuses that.
pip install -r requirements.txt

echo "==> Downloading TorrServer $TS_TAG"
case "$(uname -m)" in
  aarch64 | arm64) ts_arch=arm64 ;;
  armv7l | armv8l | arm) ts_arch=arm7 ;;
  x86_64) ts_arch=amd64 ;;
  i686 | i386) ts_arch=386 ;;
  *) echo "Unsupported CPU: $(uname -m)"; exit 1 ;;
esac
mkdir -p bin data/torrserver cache
curl -fL -o bin/torrserver.new "https://github.com/YouROK/TorrServer/releases/download/$TS_TAG/TorrServer-android-$ts_arch"
chmod +x bin/torrserver.new
mv -f bin/torrserver.new bin/torrserver

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
  sed -i "s|^HOST=.*|HOST=0.0.0.0|" .env
fi
echo "Keys and logins can also be entered later on the web: Settings (login admin / admin)."
bash setup-keys.sh .env || true  # asks only for what is still missing
fi  # end of the full install
cd "$DIR"

echo "==> Installing services"
# runit service: `run` must stay in the foreground; runit restarts it whenever it exits.
add_service() {
  local name="$1" command="$2"
  mkdir -p "$SV/$name/log"
  cat > "$SV/$name/run" <<RUN
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
cd "$DIR"
exec $command
RUN
  chmod +x "$SV/$name/run"
  ln -sf "$PREFIX/share/termux-services/svlogger" "$SV/$name/log/run"  # -> \$PREFIX/var/log/sv/$name/current
  rm -f "$SV/$name/down"  # enabled: starts with the service daemon
}
add_service streamfinder-torrserver "./bin/torrserver -p 8090 -d \"$DIR/data/torrserver\" -k"
add_service streamfinder "gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 --timeout 120 app:app"
# Checks GitHub every 30 minutes and installs a new version when there is one.
add_service streamfinder-updater "sh -c 'sleep 60; while true; do $PREFIX/bin/streamfinder auto-update; sleep 1800; done'"

echo "==> Installing the streamfinder command"
# Written next to it and moved into place: `streamfinder update` runs this while the old one is running.
cat > "$PREFIX/bin/streamfinder.new" <<'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# streamfinder start | stop | restart | status | update | auto-update | logs
set -uo pipefail
DIR="$HOME/stream-finder-torrent"
export SVDIR="${SVDIR:-$PREFIX/var/service}"  # `sv` also works from inside a service
SERVICES=(streamfinder-torrserver streamfinder streamfinder-updater)
# Local Whisper for AI subtitles, when installed (install-whisper.sh)
[ -d "$PREFIX/var/service/streamfinder-whisper" ] && SERVICES+=(streamfinder-whisper)

# Install the newest version from GitHub (shallow clone: fetch + reset, works after force-pushes).
# By hand it also asks for keys that are still missing (the update service has no terminal).
update() {
  git -C "$DIR" fetch --depth 1 origin master || return 1
  local keys=0
  if [ "$(git -C "$DIR" rev-parse HEAD)" = "$(git -C "$DIR" rev-parse FETCH_HEAD)" ]; then
    bash "$DIR/setup-keys.sh" "$DIR/.env" || keys=$?
    echo "$(date '+%F %T') already up to date ($(git -C "$DIR" rev-parse --short HEAD))"
    if [ "$keys" = 2 ]; then ensure_daemon; sv restart streamfinder; fi
    return 0
  fi
  git -C "$DIR" reset --hard FETCH_HEAD || return 1
  pip install -q -r "$DIR/requirements.txt" || return 1
  # The new version may bring new services or a new version of this command.
  bash "$DIR/termux-install.sh" --refresh || true
  bash "$DIR/setup-keys.sh" "$DIR/.env" || true
  ensure_daemon
  sv restart streamfinder
  echo "$(date '+%F %T') updated to $(git -C "$DIR" rev-parse --short HEAD)"
}

phone_ip() {
  python - <<'PY'
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the Wi-Fi address
    print(s.getsockname()[0])
except OSError:
    print("127.0.0.1")
PY
}

# The runit supervisor normally starts with every Termux session; make sure it runs now.
ensure_daemon() {
  if ! pgrep -f "runsvdir $PREFIX/var/service" >/dev/null; then
    service-daemon start >/dev/null 2>&1 || true
    sleep 2
  fi
}

start() {
  termux-wake-lock 2>/dev/null || true  # keep running with the screen off
  ensure_daemon
  for name in "${SERVICES[@]}"; do sv up "$name"; done
  sleep 3
  status
  echo
  echo "Open on this phone:  http://127.0.0.1:5000"
  echo "Open on your network: http://$(phone_ip):5000"
}

stop() {
  for name in "${SERVICES[@]}"; do sv down "$name"; done
  termux-wake-unlock 2>/dev/null || true
}

status() {
  ensure_daemon
  for name in "${SERVICES[@]}"; do sv status "$name"; done
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) ensure_daemon; for name in "${SERVICES[@]}"; do sv restart "$name"; done; status ;;
  status) status ;;
  update) update && status ;;
  auto-update) update ;;  # used by the streamfinder-updater service
  logs) tail -n 50 -f "$PREFIX"/var/log/sv/streamfinder*/current ;;
  *) echo "usage: streamfinder start|stop|restart|status|update|logs"; exit 1 ;;
esac
SCRIPT
chmod +x "$PREFIX/bin/streamfinder.new"
mv -f "$PREFIX/bin/streamfinder.new" "$PREFIX/bin/streamfinder"

# After a reboot (needs the Termux:Boot app, opened once): take the wake lock and start the
# service supervisor, which brings both services up.
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/streamfinder" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
. "$PREFIX/etc/profile"
BOOT
chmod +x "$HOME/.termux/boot/streamfinder"

# Local Whisper for AI subtitles - on by default (opt out: touch cache/.no-whisper).
if [ ! -x "$HOME/whisper.cpp/build/bin/whisper-server" ] && [ ! -f "$DIR/cache/.no-whisper" ]; then
  bash "$DIR/install-whisper.sh" || echo "!! Local Whisper could not be installed - AI subtitles can use a Groq key instead (Settings)."
fi
# Already installed: run it the way this version wants (repo's whisper-run.sh).
WSV="$PREFIX/var/service/streamfinder-whisper"
if [ -x "$HOME/whisper.cpp/build/bin/whisper-server" ] && [ -d "$WSV" ] && ! grep -q "whisper-run.sh" "$WSV/run" 2>/dev/null; then
  printf '#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
export WHISPER_DIR="%s" WHISPER_PORT=8178
exec sh %s/whisper-run.sh
' "$HOME/whisper.cpp" "$DIR" > "$WSV/run"
  chmod +x "$WSV/run"
  sv restart streamfinder-whisper >/dev/null 2>&1 || true
elif [ -d "$WSV" ]; then
  sv restart streamfinder-whisper >/dev/null 2>&1 || true  # new run settings come with the update
fi
if [ -x "$HOME/whisper.cpp/build/bin/whisper-server" ] && ! grep -q '^TRANSCRIBE_PROVIDER=' "$DIR/.env"; then
  { echo "TRANSCRIBE_PROVIDER=local"; echo "TRANSCRIBE_API_URL=http://127.0.0.1:8178/v1"; } >> "$DIR/.env"
fi

if [ "$REFRESH" = 1 ]; then
  ensure_daemon() { pgrep -f "runsvdir $PREFIX/var/service" >/dev/null || service-daemon start >/dev/null 2>&1 || true; }
  ensure_daemon
  sleep 1
  sv up streamfinder-updater >/dev/null 2>&1 || true
  exit 0
fi

streamfinder start
echo
echo "StreamFinder now runs permanently: it restarts by itself if it crashes and updates itself from GitHub."
echo "For autostart after a reboot install Termux:Boot from F-Droid and open it once."
echo "Also disable battery optimisation for Termux in Android settings."
