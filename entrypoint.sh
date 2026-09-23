#!/bin/sh
set -e
echo "[entrypoint] updating yt-dlp..."
pip install --no-cache-dir --upgrade yt-dlp
yt-dlp --version
exec python3 /app/app.py
