#!/data/data/com.termux/files/usr/bin/bash
# StreamFinder on an Android phone (Termux), one command:
#   curl -fsSL https://raw.githubusercontent.com/poruto/stream-finder-torrent/master/termux-install.sh | bash
#
# Installs Python, git, ffmpeg (audio conversion), the app and TorrServer, then adds a
# `streamfinder` command: streamfinder start | stop | status | update | logs
set -euo pipefail

REPO_URL="https://github.com/poruto/stream-finder-torrent.git"
DIR="$HOME/stream-finder-torrent"
TS_TAG="${TORRSERVER_TAG:-MatriX.147}"

echo "==> Installing packages"
# --force-confnew: never stop and ask about changed config files (stdin is the curl pipe).
APT_OPTS=(-y -o Dpkg::Options::=--force-confnew)
pkg update "${APT_OPTS[@]}"
pkg install "${APT_OPTS[@]}" python git curl ffmpeg   # ffmpeg includes ffprobe

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
curl -fL -o bin/torrserver "https://github.com/YouROK/TorrServer/releases/download/$TS_TAG/TorrServer-android-$ts_arch"
chmod +x bin/torrserver

if [ ! -f .env ]; then
  cp .env.example .env
  read -r -p "TMDB API key (https://www.themoviedb.org/settings/api): " tmdb_key </dev/tty
  read -r -p "OpenSubtitles API key (optional, Enter to skip): " os_key </dev/tty
  sed -i "s|^TMDB_API_KEY=.*|TMDB_API_KEY=${tmdb_key}|" .env
  sed -i "s|^OPENSUBTITLES_API_KEY=.*|OPENSUBTITLES_API_KEY=${os_key}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
  sed -i "s|^HOST=.*|HOST=0.0.0.0|" .env
fi

echo "==> Installing the streamfinder command"
cat > "$PREFIX/bin/streamfinder" <<'SCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# streamfinder start | stop | restart | status | update | logs
set -uo pipefail
DIR="$HOME/stream-finder-torrent"
RUN="$DIR/data/run"
mkdir -p "$RUN"

running() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

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

start() {
  command -v termux-wake-lock >/dev/null && termux-wake-lock  # keep running with the screen off
  cd "$DIR"
  if ! running torrserver; then
    nohup ./bin/torrserver -p 8090 -d "$DIR/data/torrserver" -k > "$RUN/torrserver.log" 2>&1 &
    echo $! > "$RUN/torrserver.pid"
  fi
  if ! running app; then
    nohup gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 --timeout 120 app:app > "$RUN/app.log" 2>&1 &
    echo $! > "$RUN/app.pid"
  fi
  sleep 3
  status
  echo "Open on this phone:  http://127.0.0.1:5000"
  echo "Open on your network: http://$(phone_ip):5000"
}

stop() {
  for name in app torrserver; do
    running "$name" && kill "$(cat "$RUN/$name.pid")" && echo "stopped $name"
    rm -f "$RUN/$name.pid"
  done
  command -v termux-wake-unlock >/dev/null && termux-wake-unlock
}

status() {
  for name in torrserver app; do
    if running "$name"; then echo "$name: running"; else echo "$name: stopped"; fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  update)
    git -C "$DIR" fetch --depth 1 origin master && git -C "$DIR" reset --hard origin/master \
      && pip install -r "$DIR/requirements.txt" && stop && start ;;
  logs) tail -n 50 -f "$RUN/app.log" "$RUN/torrserver.log" ;;
  *) echo "usage: streamfinder start|stop|restart|status|update|logs"; exit 1 ;;
esac
SCRIPT
chmod +x "$PREFIX/bin/streamfinder"

# Autostart when the phone boots (only if the Termux:Boot app is installed).
mkdir -p "$HOME/.termux/boot"
printf '#!/data/data/com.termux/files/usr/bin/sh\nstreamfinder start\n' > "$HOME/.termux/boot/streamfinder"
chmod +x "$HOME/.termux/boot/streamfinder"

streamfinder start
