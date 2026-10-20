#!/data/data/com.termux/files/usr/bin/bash
# StreamFinder on an Android phone (Termux), one command:
#   curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/termux-install.sh | bash
#
# Installs Python, git, ffmpeg (audio conversion), the app and TorrServer and runs both
# as always-on services (termux-services / runit: restarted if they crash, started again
# after a reboot with Termux:Boot). Control them with:
#   streamfinder start | stop | restart | status | update | logs
set -euo pipefail

REPO_URL="https://github.com/poruto/stream-finder-torrent.git"
DIR="$HOME/stream-finder-torrent"
TS_TAG="${TORRSERVER_TAG:-MatriX.147}"
SV="$PREFIX/var/service"

echo "==> Installing packages"
# --force-confnew: never stop and ask about changed config files (stdin is the curl pipe).
APT_OPTS=(-y -o Dpkg::Options::=--force-confnew)
pkg update "${APT_OPTS[@]}"
pkg install "${APT_OPTS[@]}" python git curl ffmpeg termux-services procps   # ffmpeg includes ffprobe

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
  read -r -p "TMDB API key (https://www.themoviedb.org/settings/api): " tmdb_key </dev/tty
  read -r -p "OpenSubtitles API key (optional, Enter to skip): " os_key </dev/tty
  sed -i "s|^TMDB_API_KEY=.*|TMDB_API_KEY=${tmdb_key}|" .env
  sed -i "s|^OPENSUBTITLES_API_KEY=.*|OPENSUBTITLES_API_KEY=${os_key}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
  sed -i "s|^HOST=.*|HOST=0.0.0.0|" .env
fi

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

echo "==> Installing the streamfinder command"
cat > "$PREFIX/bin/streamfinder" <<'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# streamfinder start | stop | restart | status | update | logs
set -uo pipefail
DIR="$HOME/stream-finder-torrent"
SERVICES=(streamfinder-torrserver streamfinder)

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
  update)
    git -C "$DIR" fetch --depth 1 origin master && git -C "$DIR" reset --hard origin/master \
      && pip install -r "$DIR/requirements.txt" && ensure_daemon && sv restart streamfinder && status ;;
  logs) tail -n 50 -f "$PREFIX/var/log/sv/streamfinder/current" "$PREFIX/var/log/sv/streamfinder-torrserver/current" ;;
  *) echo "usage: streamfinder start|stop|restart|status|update|logs"; exit 1 ;;
esac
SCRIPT
chmod +x "$PREFIX/bin/streamfinder"

# After a reboot (needs the Termux:Boot app, opened once): take the wake lock and start the
# service supervisor, which brings both services up.
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/streamfinder" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
. "$PREFIX/etc/profile"
BOOT
chmod +x "$HOME/.termux/boot/streamfinder"

streamfinder start
echo
echo "StreamFinder now runs permanently: it restarts by itself if it crashes."
echo "For autostart after a reboot install Termux:Boot from F-Droid and open it once."
echo "Also disable battery optimisation for Termux in Android settings."
