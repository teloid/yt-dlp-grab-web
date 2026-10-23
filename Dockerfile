FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The container runs as UID 1000 and upgrades yt-dlp on every start. Newer yt-dlp wheels also install
# shell completions and a man page under /usr/local/share, so that has to be writable too.
RUN pip install --no-cache-dir flask yt-dlp \
    && chown -R 1000:1000 /usr/local/lib/python3.12/site-packages /usr/local/bin /usr/local/share

WORKDIR /app
COPY app.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh && chown -R 1000:1000 /app

ENV HOME=/data
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
