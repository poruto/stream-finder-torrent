#!/bin/sh
# Starts the whisper.cpp server for AI subtitles (the streamfinder-whisper service runs this).
# Lives in the repo so that updates can change how the server runs.
#   models/ggml-current.bin  link to the chosen model (Settings -> Whisper quality)
DIR="${WHISPER_DIR:-$HOME/whisper.cpp}"
PORT="${WHISPER_PORT:-8178}"
model=$(readlink -f "$DIR/models/ggml-current.bin")

# Fast cores only: phones with 8 cores (e.g. Galaxy S24) have 6 fast ones, the rest
# would slow everything down; PCs and servers use all cores.
cores=$(nproc 2>/dev/null || echo 4)
case "${PREFIX:-}" in
  *com.termux*) if [ "$cores" -ge 8 ]; then threads=6; elif [ "$cores" -ge 4 ]; then threads=4; else threads=$cores; fi ;;
  *) threads=$cores ;;
esac

# Exact word timing (DTW) needs the model's own preset and flash attention off.
preset=$(basename "$model" .bin); preset=${preset#ggml-}; preset=${preset%%-q*}
case "$preset" in
  tiny | base | small | medium) set -- --dtw "$preset" -nfa ;;
  *) set -- ;;
esac

# -nf: no temperature fallback - music scenes would otherwise be decoded again and again.
exec "$DIR/build/bin/whisper-server" -m "$model" --host 127.0.0.1 --port "$PORT" -l auto \
  -t "${WHISPER_THREADS:-$threads}" --inference-path /v1/audio/transcriptions -nf "$@"
