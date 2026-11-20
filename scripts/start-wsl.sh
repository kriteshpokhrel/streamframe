#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$0")/.."
tools="$HOME/.streamframe-tools"
venv="$tools/venv"

ffmpeg="${STREAMFRAME_FFMPEG_PATH:-${STREAMFRAME_FFMPEG:-}}"
if [[ -z "$ffmpeg" ]]; then
    for bundled in "$tools"/ffmpeg-*-linux64-gpl/bin/ffmpeg; do
        if [[ -x "$bundled" ]]; then
            ffmpeg="$bundled"
            break
        fi
    done
fi
if [[ -z "$ffmpeg" ]]; then
    ffmpeg="$(command -v ffmpeg || true)"
fi
if [[ -z "$ffmpeg" || ! -x "$ffmpeg" || ! -x "$(dirname -- "$ffmpeg")/ffprobe" ]]; then
    echo "Install FFmpeg and ffprobe inside WSL (see START.md)." >&2
    exit 1
fi
export PATH="$(dirname -- "$ffmpeg"):$PATH"
export STREAMFRAME_FFMPEG_PATH="$ffmpeg"

if [[ ! -x "$venv/bin/python" ]] && ! command -v python3.10 >/dev/null; then
    echo "Install Python 3.10 inside WSL (see START.md)." >&2
    exit 1
fi
if [[ "${1:-}" == check ]]; then
    exit 0
fi

if [[ ! -f "$venv/.streamframe-setup-complete" ]]; then
    if [[ ! -x "$venv/bin/python" ]]; then
        python3.10 -m venv "$venv"
    fi
    if ! "$venv/bin/python" -c 'import cv2, fastapi, faster_whisper, uvicorn, yt_dlp' >/dev/null 2>&1; then
        "$venv/bin/python" -m pip install 'pip==25.3'
        "$venv/bin/python" -m pip install -r requirements.lock
    fi
    "$venv/bin/python" -m pip check
    touch "$venv/.streamframe-setup-complete"
fi

extractor_pin="$(grep -m1 '^yt-dlp==' requirements.lock)"
installed_extractor="$("$venv/bin/python" -m yt_dlp --version 2>/dev/null || true)"
if [[ "yt-dlp==$installed_extractor" != "$extractor_pin" ]]; then
    "$venv/bin/python" -m pip install --no-deps "$extractor_pin"
    "$venv/bin/python" -m pip install --no-deps -e .
    "$venv/bin/python" -m pip check
fi

export STREAMFRAME_DATA_DIR="${STREAMFRAME_DATA_DIR:-$HOME/.streamframe}"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$venv/bin/python" -m streamframe.cli serve
