#!/bin/sh
SP=/usr/local/lib/python3.12/site-packages

echo "[entrypoint] updating yt-dlp..."
rm -rf "$SP"/~*   # leftovers from a pip run that was killed mid-install
pip install --no-cache-dir --upgrade yt-dlp || echo "[entrypoint] upgrade failed, keeping the installed version"

# A failed upgrade must never leave the app unstartable (that is a restart loop).
if ! yt-dlp --version; then
  echo "[entrypoint] yt-dlp is broken, reinstalling"
  rm -rf "$SP"/~*
  pip install --no-cache-dir --force-reinstall yt-dlp
  yt-dlp --version || exit 1
fi

exec python3 /app/app.py
