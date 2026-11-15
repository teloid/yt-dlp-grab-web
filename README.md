# 🕯️ Grab

A tiny, self-hosted YouTube (and anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports) downloader with a cozy web UI. Built to run on a Raspberry Pi or any small Docker host.

- **Nothing is kept longer than it takes you to grab it.** Each download lives in a temporary per-job folder; once your browser has the complete file, the server's copy is deleted. Cancel a running job and it's wiped instantly.
- **History for forgotten downloads.** Closed the tab before saving? Finished files wait for you — reopen the page (even after a container restart) and they're listed with their title, age and size, ready to save or discard. They're kept until you save or discard them; there's no automatic expiry.
- **Quality presets** for video (Best/4K/1440p/1080p/720p/480p/360p) and MP3 (Best/320k/256k/192k/128k).
- **iPhone-friendly mode** (off by default — turn it on when a file won't play on your iPhone). Sources like YouTube (often AV1) and Instagram can hand back video that iPhones won't play in the built-in player or import into Photos. This mode prefers H.264 where the site offers it, then inspects the finished file and, only if needed, converts it to H.264 + AAC (or just fixes the HEVC tag, which is instant). Files that are already fine are left untouched. Conversion shows live progress and can be cancelled; on a Raspberry Pi the conversion is slow (roughly real-time or worse, and it pegs the CPU), which is why it's opt-in. Leave it off for the untouched original.
- **Codec readout** on every finished card (e.g. `H.264 + AAC · 1080×1920`), so you can see exactly what you're getting.
- **Optional thumbnail embedding** and/or **saving the thumbnail as its own file** — independent toggles, either, both, or neither.
- **Cancel button** that actually kills the underlying `yt-dlp`/`ffmpeg` process and wipes the partial file — not just a UI dismiss.
- **Cookies** upload straight from the browser (Settings tab) for age-restricted / members-only content.
- **yt-dlp updates itself** on every container start (`pip install --upgrade yt-dlp`), so a quick restart keeps you current with YouTube's cat-and-mouse extraction changes.

## Why no server-side storage?

Free "YouTube downloader" sites either link straight to YouTube's own low-quality progressive stream (no processing at all), or they run the exact same kind of job this container does — download, merge, mux — just usually without deleting anything afterward, and usually behind a wall of ads. This project does the real work (merges separate video+audio streams via `ffmpeg`, same as any decent-quality downloader must), but only for as long as it takes to hand the file to you, and then it's gone.

## Screenshot-in-words

A dark, warm, candlelit page: paste a link, flip a couple of toggles (audio-only? embed thumbnail? which quality?), hit Download. A progress bar animates while it works, then a glowing "Save to your device" button appears — click it and your browser streams the file down and the copy on the server disappears.

## Requirements

- Docker + Docker Compose v2 (`docker compose`, not the old `docker-compose`)
- Any architecture — tested on a Raspberry Pi 4 (arm64) and plain x86_64
- Outbound internet access (to reach YouTube, and to fetch yt-dlp's [EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) signature-solver component from GitHub)

## Quick start

```bash
git clone https://github.com/teloid/yt-dlp-grab-web.git
cd yt-dlp-grab-web
docker compose up -d --build
```

Open `http://<your-host>:8090`.

That's it — paste a link, pick your options, download.

## Configuration

### Port

Default is `8090`. Change the left side of the port mapping in `docker-compose.yml` if it's taken:

```yaml
ports:
  - "8090:8090"   # host:container — change the first number
```

### File ownership (UID)

The container runs as UID `1000` so files it touches are readable/deletable by a normal first-created Linux user (this is the default UID for both a fresh Raspberry Pi's `pi` user and most single-user Linux boxes). If your host user has a different UID, either:

- Create/adjust a user with UID 1000 to own `./data`, or
- Edit `user: "1000:1000"` in `docker-compose.yml` to match `id -u`:`id -g` of the user who should own downloaded/cookie files, then rebuild.

### Cookies

For age-restricted or members-only videos, export your browser's cookies (e.g. with the ["Get cookies.txt LOCALLY"](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension) and upload the resulting `cookies.txt` from the **Settings** tab in the app. It's stored at `./data/cookies.txt` on the host — back it up or `.gitignore` it if you fork this repo with your own data alongside it (already covered by this repo's `.gitignore`).

### Keeping yt-dlp up to date

yt-dlp gets upgraded automatically every time the container starts, via `pip install --upgrade yt-dlp` in the entrypoint. To pick up a new version without any other changes:

```bash
docker compose restart yt-dlp
```

### Storage layout

```
yt-dlp-grab-web/
├── data/            # cookies.txt + EJS cache — the only thing that persists
└── scratch/         # one folder per download; empty when nothing is waiting for you
```

Each folder in `scratch/` holds a download plus a small `job.json` describing it. A folder is deleted as soon as everything in it has been saved (or you discard it). Finished-but-unsaved folders are what the history shows, and they survive restarts. On startup the app deletes anything that can't be resumed (a download that was mid-flight when the container stopped, or a folder whose files are gone), and a background sweep every 10 minutes kills and removes hung or failed jobs older than 3 hours. Finished downloads are never swept — if disk space matters, glance at the "ready to save · N MB waiting on the Pi" line in the UI and discard what you don't need.

## How it works, briefly

1. You submit a URL + options. A background thread runs `yt-dlp` as a real subprocess (not the Python library), with your chosen format/quality flags, into a fresh temp folder in its own process group.
2. The UI polls for progress, parsed straight out of yt-dlp's own `[download] NN%` / `[Merger]` / `[EmbedThumbnail]` log lines.
3. When it's done, the job is written to disk (`job.json`) and the "Save to your device" button streams the file to your browser. Streaming deletes nothing: a dropped connection or a locked phone just means you tap Save again. Only after the browser confirms it received every byte does it call `POST /files/<id>/ack`, and that's what deletes the server's copy. (The server can't tell by itself — small files vanish into socket buffers instantly, so "finished sending" doesn't mean "received".) Cancelling a running job sends `SIGTERM` to the whole process group first so a mid-merge `ffmpeg` child doesn't linger, then wipes the folder.

## Troubleshooting

**"Sign in to confirm you're not a bot"** — YouTube is rejecting the request. Upload fresh cookies in Settings (export them from a private/incognito window, then close it, so the browser doesn't rotate the session out from under the file). A stale or rotated `cookies.txt` is the usual cause.

**Downloaded video won't play on iPhone / "Save to Photos" does nothing** — turn on *iPhone-friendly (H.264)* (it's off by default) and check the codec readout on the finished card — it should say `H.264 + AAC`. If it does and the file still misbehaves, try again with *Embed thumbnail* off.

**The 📋 paste button shows a hint instead of pasting** — browsers only allow one-tap clipboard reads on HTTPS (or localhost). Over plain `http://` on your LAN, long-press the field and tap *Paste* instead. Serving the app over HTTPS (e.g. via a reverse proxy or Tailscale) makes the button work.

## Security notes

This runs Flask's built-in development server — fine for a LAN-only personal tool, **not meant to be exposed to the internet**. It has no authentication. If you need remote access, put it behind a VPN (which, if you're the type of person running this on a Pi, you probably already have) rather than port-forwarding it.

## License

No license file yet — treat as "personal project, ask before reusing commercially."
