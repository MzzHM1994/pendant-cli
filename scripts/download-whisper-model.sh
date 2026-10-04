#!/usr/bin/env bash
# Download one whisper.cpp model onto this Mac.
# Transcription itself never uses the network. This script is the
# one-time setup download. See README-MACOS.md.

set -euo pipefail

MODEL="${1:-small}"
case "$MODEL" in
  tiny|tiny.en|base|base.en|small|small.en|medium|medium.en|large-v3|large-v3-turbo)
    ;;
  *)
    echo "Unknown model '$MODEL'." >&2
    echo "Choose one of: tiny, base, small, medium, large-v3" >&2
    exit 1
    ;;
esac

DEST="${PENDANT_MODEL_DIR:-$HOME/Library/Application Support/pendant-cli/models}"
mkdir -p "$DEST"
OUT="$DEST/ggml-${MODEL}.bin"

if [[ -f "$OUT" ]]; then
  echo "Already downloaded: $OUT"
  exit 0
fi

URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin"
echo "Downloading the '$MODEL' speech model onto this Mac."
echo "This is the only network step. After this, transcription stays offline."
echo "Saving to: $OUT"

TMP="$OUT.partial"
rm -f "$TMP"
curl -L --fail --progress-bar -o "$TMP" "$URL"
mv "$TMP" "$OUT"
echo "Saved $OUT"
