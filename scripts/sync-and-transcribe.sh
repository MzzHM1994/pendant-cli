#!/usr/bin/env bash
# Download pendant recordings and write local transcripts.
# Usage: scripts/sync-and-transcribe.sh BLUETOOTH-ADDRESS [extra pendant-cli flags]
# Find the address with: pendant scan

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/sync-and-transcribe.sh BLUETOOTH-ADDRESS"
  echo "Example: scripts/sync-and-transcribe.sh AA:BB:CC:DD:EE:FF"
  echo "Find the address with: pendant scan"
  exit 1
fi

missing=0
if ! command -v ffmpeg >/dev/null 2>&1 && [[ ! -x /opt/homebrew/bin/ffmpeg ]]; then
  echo "ffmpeg is missing. Install it with: brew install ffmpeg"
  missing=1
fi
if ! command -v whisper-cli >/dev/null 2>&1 && [[ ! -x /opt/homebrew/bin/whisper-cli ]]; then
  echo "whisper.cpp is missing. Install it with: brew install whisper.cpp"
  missing=1
fi
if [[ "$missing" -ne 0 ]]; then
  exit 1
fi

ADDRESS="$1"
shift
exec pendant sync-and-transcribe "$ADDRESS" "$@"
