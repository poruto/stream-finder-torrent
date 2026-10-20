#!/usr/bin/env bash
# Local AI subtitles: build whisper.cpp and run its server on 127.0.0.1:8178 (offline, free).
#   bash install-whisper.sh            # Termux (phone) or Debian/Ubuntu/Raspberry Pi OS
#   WHISPER_MODEL=small-q5_1 bash install-whisper.sh   # better with muffled speech, slower
# Runs again safely: updates whisper.cpp, keeps the downloaded models and the choice.
# Models: tiny-q5_1 (30 MB, fastest - default), base-q5_1 (60 MB), small-q5_1 (190 MB).
# The quality can also be switched on the Settings page; the server loads
# models/ggml-current.bin, a link to the chosen one.
set -euo pipefail

MODEL="${WHISPER_MODEL:-}"
DIR="${WHISPER_DIR:-$HOME/whisper.cpp}"
PORT="${WHISPER_PORT:-8178}"
TERMUX=0
[[ "${PREFIX:-}" == *com.termux* ]] && TERMUX=1

echo "==> Local Whisper for AI subtitles"
if [ "$(uname -m)" = "armv7l" ]; then
  echo "!! This is a 32-bit ARM board (e.g. Raspberry Pi 3): Whisper will be slower than"
  echo "!! the film plays. A Groq key, or Whisper on a PC or a phone, works much better."
fi

if [ "$TERMUX" = 1 ]; then
  pkg install -y -o Dpkg::Options::=--force-confnew clang cmake make git curl
else
  sudo apt-get update -qq
  sudo apt-get install -y -qq build-essential cmake git curl
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --depth 1 origin master && git -C "$DIR" reset --hard FETCH_HEAD
else
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$DIR"
fi

echo "==> Building the server (a few minutes the first time)"
cmake -S "$DIR" -B "$DIR/build" -DCMAKE_BUILD_TYPE=Release \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF -DBUILD_SHARED_LIBS=OFF >/dev/null
cmake --build "$DIR/build" -j "$(nproc 2>/dev/null || echo 4)" --target whisper-server
SERVER="$DIR/build/bin/whisper-server"

mkdir -p "$DIR/models"
CURRENT="$DIR/models/ggml-current.bin"
if [ -z "$MODEL" ]; then
  # Keep what was picked before (Settings), else the fastest model.
  if [ -e "$CURRENT" ]; then
    MODEL="$(basename "$(readlink -f "$CURRENT")" .bin)"; MODEL="${MODEL#ggml-}"
  else
    MODEL="tiny-q5_1"
  fi
fi
if [ ! -s "$DIR/models/ggml-$MODEL.bin" ]; then
  echo "==> Downloading the $MODEL model"
  curl -fL -o "$DIR/models/ggml-$MODEL.bin.part" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL.bin"
  mv -f "$DIR/models/ggml-$MODEL.bin.part" "$DIR/models/ggml-$MODEL.bin"
fi
ln -sfn "ggml-$MODEL.bin" "$CURRENT"

# Phones: the 4 big cores; elsewhere all cores.
THREADS=$(( $(nproc 2>/dev/null || echo 4) > 4 ? 4 : $(nproc 2>/dev/null || echo 4) ))
[ "$TERMUX" = 0 ] && THREADS=$(nproc 2>/dev/null || echo 4)
# The server is started through a wrapper that reads which model is linked:
# DTW word timing (exact subtitle starts) needs the model's own preset
# (tiny/base/small), and flash attention off.
RUNNER="$DIR/run-server.sh"
cat > "$RUNNER" <<WRAP
#!/bin/sh
model=\$(readlink -f "$CURRENT")
preset=\$(basename "\$model" .bin); preset=\${preset#ggml-}; preset=\${preset%%-q*}
case "\$preset" in
  tiny | base | small | medium) set -- --dtw "\$preset" -nfa ;;
  *) set -- ;;
esac
exec "$SERVER" -m "\$model" --host 127.0.0.1 --port $PORT -l auto -t $THREADS \\
  --inference-path /v1/audio/transcriptions "\$@"
WRAP
chmod +x "$RUNNER"

if [ "$TERMUX" = 1 ]; then
  SV="$PREFIX/var/service/streamfinder-whisper"
  mkdir -p "$SV/log"
  cat > "$SV/run" <<RUN
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
exec $RUNNER
RUN
  chmod +x "$SV/run"
  ln -sf "$PREFIX/share/termux-services/svlogger" "$SV/log/run"
  rm -f "$SV/down"
  pgrep -f "runsvdir $PREFIX/var/service" >/dev/null || service-daemon start >/dev/null 2>&1 || true
  sleep 2
  sv restart streamfinder-whisper >/dev/null 2>&1 || sv up streamfinder-whisper || true
else
  sudo tee /etc/systemd/system/streamfinder-whisper.service >/dev/null <<UNIT
[Unit]
Description=Whisper server for StreamFinder AI subtitles
After=network.target

[Service]
User=$USER
ExecStart=$RUNNER
Restart=always
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now streamfinder-whisper
  sudo systemctl restart streamfinder-whisper
fi

for _ in $(seq 1 30); do
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then
    echo "Local Whisper is running on http://127.0.0.1:$PORT/v1"
    exit 0
  fi
  sleep 1
done
echo "!! The Whisper server did not start - see its log." >&2
exit 1
