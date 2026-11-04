#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

python local_usb_camera_stream.py \
  --index "${CAMERA_INDEX:-0}" \
  --host "${CAMERA_HOST:-0.0.0.0}" \
  --port "${CAMERA_PORT:-18081}" \
  --enable-audio \
  --sample-rate "${AUDIO_SAMPLE_RATE:-16000}" \
  --channels "${AUDIO_CHANNELS:-1}" \
  --audio-seconds "${AUDIO_SECONDS:-5}"
