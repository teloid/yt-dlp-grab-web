# 🕯️ Grab

A tiny, self-hosted YouTube (and anything else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports) downloader with a cozy web UI. Built to run on a Raspberry Pi or any small Docker host.

- **No files are kept on the server.** Each download lives in a temporary per-job folder just long enough to process, then streams straight to your browser and is deleted the moment the transfer finishes (or the instant you cancel it).
- **Quality presets** for video (Best/4K/1440p/1080p/720p/480p/360p) and MP3 (Best/320k/256k/192k/128k).
- **iPhone-friendly mode** (H.264). YouTube's "best" MP4 is often AV1, which most iPhones can't play in the built-in player or import into Photos. The toggle prefers H.264 instead (capped at 1080p, which is the most H.264 YouTube offers) and switches itself on automatically when you open the page on an iPhone/iPad.
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
└── scratch/         # per-download temp folders — should always end up empty
```

`scratch/` is wiped per-job as soon as a file is delivered to your browser, plus a background sweep every 10 minutes that kills and removes anything older than 3 hours (covers crashed jobs). If you ever see stray folders under `scratch/`, they're safe to delete manually — the container never counts on anything surviving there.

## How it works, briefly

1. You submit a URL + options. A background thread runs `yt-dlp` as a real subprocess (not the Python library), with your chosen format/quality flags, into a fresh temp folder in its own process group.
2. The UI polls for progress, parsed straight out of yt-dlp's own `[download] NN%` / `[Merger]` / `[EmbedThumbnail]` log lines.
3. When it's done, the "Save to your device" button streams the file to your browser via a generator response — the moment that stream finishes, a `finally` block deletes the file (and cleans up the job) server-side. Cancelling does the same thing, except it also sends `SIGTERM` to the whole process group first so a mid-merge `ffmpeg` child doesn't linger.

## Troubleshooting

**"Sign in to confirm you're not a bot"** — YouTube is rejecting the request. Upload fresh cookies in Settings (export them from a private/incognito window, then close it, so the browser doesn't rotate the session out from under the file). A stale or rotated `cookies.txt` is the usual cause.

**Downloaded video won't play on iPhone / "Save to Photos" does nothing** — turn on *iPhone-friendly (H.264)* (it's on by default when opened from an iPhone). If it still misbehaves, try again with *Embed thumbnail* off.

**The 📋 paste button shows a hint instead of pasting** — browsers only allow one-tap clipboard reads on HTTPS (or localhost). Over plain `http://` on your LAN, long-press the field and tap *Paste* instead. Serving the app over HTTPS (e.g. via a reverse proxy or Tailscale) makes the button work.

## Security notes

This runs Flask's built-in development server — fine for a LAN-only personal tool, **not meant to be exposed to the internet**. It has no authentication. If you need remote access, put it behind a VPN (which, if you're the type of person running this on a Pi, you probably already have) rather than port-forwarding it.

## License

No license file yet — treat as "personal project, ask before reusing commercially."
