import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, request, redirect, url_for, Response, jsonify, render_template_string

SCRATCH_DIR = Path("/scratch")   # ephemeral working dir, wiped per job after delivery
DATA_DIR = Path("/data")
COOKIES_PATH = DATA_DIR / "cookies.txt"

SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # cookies.txt is tiny

# job_id -> {url, status: running|done|error, dir, file, tail, created}
JOBS = {}
JOBS_LOCK = threading.Lock()

MAX_JOB_AGE = 3 * 60 * 60  # sweep anything (dead or alive) older than this


MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}
MIME_MAP = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav",
}
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}

# "best" / "default" reproduce the exact original behavior (no extra flag at all)
VIDEO_QUALITY_FORMATS = {
    "best": "bv*[ext=mp4]+ba[ext=m4a]/mp4",
    "2160": "bv*[ext=mp4][height<=2160]+ba[ext=m4a]/mp4",
    "1440": "bv*[ext=mp4][height<=1440]+ba[ext=m4a]/mp4",
    "1080": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/mp4",
    "720": "bv*[ext=mp4][height<=720]+ba[ext=m4a]/mp4",
    "480": "bv*[ext=mp4][height<=480]+ba[ext=m4a]/mp4",
    "360": "bv*[ext=mp4][height<=360]+ba[ext=m4a]/mp4",
}
VIDEO_QUALITY_LABELS = {
    "best": "Best", "2160": "4K", "1440": "1440p", "1080": "1080p",
    "720": "720p", "480": "480p", "360": "360p",
}
VIDEO_QUALITY_HEIGHTS = {"2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480, "360": 360}


def video_format(quality: str, ios_compat: bool) -> str:
    """yt-dlp format string. With ios_compat, try H.264 (avc1) first — the
    codec every iPhone can play natively and import to Photos — and only fall
    back to the regular selection if the video has no H.264 stream at all."""
    base = VIDEO_QUALITY_FORMATS.get(quality, VIDEO_QUALITY_FORMATS["best"])
    if not ios_compat:
        return base
    h = VIDEO_QUALITY_HEIGHTS.get(quality)
    height = f"[height<={h}]" if h else ""
    return f"bv*[vcodec^=avc1][ext=mp4]{height}+ba[ext=m4a]/{base}"
AUDIO_QUALITY_ARGS = {
    "default": None, "320": "320K", "256": "256K", "192": "192K", "128": "128K",
}
AUDIO_QUALITY_LABELS = {
    "default": "Best", "320": "320k", "256": "256k", "192": "192k", "128": "128k",
}


def build_args(url: str, job_dir: Path, embed_thumbnail: bool, save_thumbnail: bool,
                audio_only: bool, video_quality: str, audio_quality: str, ios_compat: bool = False) -> list[str]:
    args = ["yt-dlp", "--newline", "--no-playlist"]
    if COOKIES_PATH.exists():
        args += ["--cookies", str(COOKIES_PATH)]
    args += ["--remote-components", "ejs:github"]
    if audio_only:
        args += ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3"]
        aq = AUDIO_QUALITY_ARGS.get(audio_quality)
        if aq:
            args += ["--audio-quality", aq]
    else:
        args += ["-f", video_format(video_quality, ios_compat), "--merge-output-format", "mp4"]
    if save_thumbnail:
        args += ["--write-thumbnail"]
    if embed_thumbnail:
        args += ["--embed-thumbnail"]
    if embed_thumbnail or save_thumbnail:
        args += ["--convert-thumbnails", "jpg"]
    args += ["-P", str(job_dir), "-o", "%(title).200B.%(ext)s", url]
    return args


def run_job(job_id: str, url: str, embed_thumbnail: bool, save_thumbnail: bool,
            audio_only: bool, video_quality: str, audio_quality: str, ios_compat: bool = False):
    job_dir = SCRATCH_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if audio_only:
        quality_label = f"MP3 · {AUDIO_QUALITY_LABELS.get(audio_quality, 'Best')}"
    else:
        quality_label = VIDEO_QUALITY_LABELS.get(video_quality, "Best")
        if ios_compat:
            quality_label += " · H.264"
    with JOBS_LOCK:
        JOBS[job_id] = {
            "url": url,
            "status": "running",
            "dir": job_dir,
            "file": None,
            "thumb": None,
            "tail": "",
            "created": time.time(),
            "quality_label": quality_label,
            "proc": None,
        }

    def pump(stream):
        buf = []
        for raw in stream:
            line = raw.decode(errors="replace").rstrip("\n")
            buf.append(line)
            buf[:] = buf[-30:]
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["tail"] = "\n".join(buf)

    try:
        proc = subprocess.Popen(
            build_args(url, job_dir, embed_thumbnail, save_thumbnail, audio_only, video_quality, audio_quality, ios_compat),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,  # own process group, so cancel can kill yt-dlp + any ffmpeg child together
        )
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["proc"] = proc
        pump(proc.stdout)
        proc.wait(timeout=60 * 60)
        ok = proc.returncode == 0
    except Exception as exc:
        ok = False
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["tail"] += f"\n[wrapper] failed: {exc}"

    with JOBS_LOCK:
        cancelled = job_id not in JOBS
    if cancelled:
        return  # cancel route already killed the process group and wiped the dir

    video_file = None
    thumb_file = None
    if ok:
        medias = [p for p in job_dir.glob("*") if p.suffix.lower() in MEDIA_EXTS]
        video_file = medias[0] if medias else None
        ok = video_file is not None
        if ok:
            thumbs = [p for p in job_dir.glob("*.jpg")]
            thumb_file = thumbs[0] if thumbs else None

    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["status"] = "done" if ok else "error"
            JOBS[job_id]["file"] = video_file
            JOBS[job_id]["thumb"] = thumb_file
    if not ok:
        shutil.rmtree(job_dir, ignore_errors=True)


def release_delivered(job_id: str):
    """Called after a file finishes streaming: drop it from the job, and
    once nothing is left to deliver, remove the whole job + its directory."""
    job_dir = None
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if job["file"] and not job["file"].exists():
            job["file"] = None
        if job["thumb"] and not job["thumb"].exists():
            job["thumb"] = None
        if job["file"] is None and job["thumb"] is None:
            job_dir = job["dir"]
            JOBS.pop(job_id, None)
    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)


def parse_progress(tail: str):
    if not tail:
        return "Starting", None
    lines = tail.splitlines()
    joined = "\n".join(lines)
    if "[EmbedThumbnail]" in joined:
        return "Embedding thumbnail", None
    if "ThumbnailsConvertor" in joined:
        return "Converting thumbnail", None
    if "[Merger]" in joined:
        return "Merging audio + video", None
    for line in reversed(lines):
        m = re.search(r"\[download\]\s+([\d.]+)% ", line)
        if m:
            return "Downloading", float(m.group(1))
    return "Starting", None


def sweep():
    while True:
        time.sleep(600)
        now = time.time()
        with JOBS_LOCK:
            stale = [jid for jid, j in JOBS.items() if now - j["created"] > MAX_JOB_AGE]
            for jid in stale:
                proc = JOBS[jid].get("proc")
                if proc and proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                shutil.rmtree(JOBS[jid]["dir"], ignore_errors=True)
                JOBS.pop(jid, None)
            known_dirs = {j["dir"].name for j in JOBS.values()}
        for d in SCRATCH_DIR.iterdir():
            if d.is_dir() and d.name not in known_dirs and now - d.stat().st_mtime > MAX_JOB_AGE:
                shutil.rmtree(d, ignore_errors=True)


threading.Thread(target=sweep, daemon=True).start()


@app.route("/")
def index():
    return render_template_string(INDEX_HTML, cookie_ok=COOKIES_PATH.exists(), page_title="Grab — YouTube Downloader")


@app.route("/download", methods=["POST"])
def download():
    url = request.form.get("url", "").strip()
    embed_thumbnail = request.form.get("embed_thumbnail") == "1"
    save_thumbnail = request.form.get("save_thumbnail") == "1"
    audio_only = request.form.get("audio_only") == "1"
    video_quality = request.form.get("video_quality", "best")
    audio_quality = request.form.get("audio_quality", "default")
    ios_compat = request.form.get("ios_compat") == "1"
    if not url:
        return jsonify({"error": "missing url"}), 400
    job_id = uuid.uuid4().hex[:10]
    threading.Thread(
        target=run_job,
        args=(job_id, url, embed_thumbnail, save_thumbnail, audio_only, video_quality, audio_quality, ios_compat),
        daemon=True,
    ).start()
    return jsonify({"id": job_id})


@app.route("/jobs.json")
def jobs_json():
    with JOBS_LOCK:
        items = sorted(JOBS.items(), key=lambda kv: kv[1]["created"], reverse=True)
        out = []
        for job_id, job in items:
            phase, percent = parse_progress(job["tail"])
            out.append({
                "id": job_id,
                "url": job["url"],
                "status": job["status"],
                "phase": "Ready" if job["status"] == "done" else ("Failed" if job["status"] == "error" else phase),
                "percent": 100 if job["status"] == "done" else percent,
                "tail": job["tail"],
                "quality_label": job.get("quality_label", ""),
                "filename": job["file"].name if job["file"] else None,
                "size": job["file"].stat().st_size if job["file"] and job["file"].exists() else None,
                "kind": "audio" if job["file"] and job["file"].suffix.lower() in AUDIO_EXTS else "video",
                "thumb_filename": job["thumb"].name if job["thumb"] else None,
                "thumb_size": job["thumb"].stat().st_size if job["thumb"] and job["thumb"].exists() else None,
            })
    return jsonify(out)


@app.route("/jobs/<job_id>", methods=["DELETE"])
def dismiss_job(job_id):
    """Dismisses a finished/errored job, or cancels one still running —
    kills yt-dlp (and any ffmpeg it spawned) and wipes the partial download."""
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if not job:
        return "", 404
    proc = job.get("proc")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    shutil.rmtree(job["dir"], ignore_errors=True)
    return "", 204


def deliver(job_id: str, key: str, mimetype: str = None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        path = job[key] if job and job["status"] == "done" else None
    if not path or not path.exists():
        return "not ready", 404
    size = path.stat().st_size
    filename = path.name
    mimetype = mimetype or MIME_MAP.get(path.suffix.lower(), "application/octet-stream")

    def generate():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            path.unlink(missing_ok=True)
            release_delivered(job_id)

    resp = Response(generate(), mimetype=mimetype)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"] = str(size)
    return resp


@app.route("/files/<job_id>")
def files(job_id):
    return deliver(job_id, "file")


@app.route("/files/<job_id>/thumbnail")
def thumbnail(job_id):
    return deliver(job_id, "thumb", "image/jpeg")


@app.route("/settings")
def settings():
    return render_template_string(SETTINGS_HTML, cookie_ok=COOKIES_PATH.exists(), page_title="Grab — Settings")


@app.route("/settings/cookies", methods=["POST"])
def upload_cookies():
    f = request.files.get("cookies")
    if f and f.filename:
        f.save(COOKIES_PATH)
    return redirect(url_for("settings"))


@app.route("/settings/cookies", methods=["DELETE"])
def delete_cookies():
    COOKIES_PATH.unlink(missing_ok=True)
    return "", 204


BASE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Cinzel:wght@600;700&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>%F0%9F%95%AF%EF%B8%8F</text></svg>">
<style>
  :root{
    --bg1:#1b130a; --bg2:#2e1f12; --bg3:#3a2a16;
    --accent:#e0a458; --accent2:#6b8f5a;
    --card:rgba(255,235,205,.06); --card-border:rgba(224,164,88,.22);
    --text:#f5ead6; --text-dim:#c7b596;
    --danger:#c9603f; --warn:#e8b656;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;min-height:100vh;}
  body{
    font-family:'Inter',-apple-system,sans-serif; color:var(--text);
    background:linear-gradient(120deg,var(--bg1),var(--bg2),var(--bg3),var(--bg1));
    background-size:300% 300%;
    animation:drift 30s ease infinite;
    display:flex; justify-content:center; padding:6vh 1rem;
    position:relative; overflow-x:hidden;
  }
  @keyframes drift{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
  .fireflies{position:fixed; inset:0; z-index:0; pointer-events:none; overflow:hidden;}
  .firefly{
    position:absolute; width:4px; height:4px; border-radius:50%;
    background:radial-gradient(circle, #ffe6b0, #e0a458 60%, transparent 100%);
    box-shadow:0 0 8px 2px rgba(224,164,88,.55);
    opacity:0; animation:driftUp ease-in-out infinite;
  }
  @keyframes driftUp{
    0%{opacity:0; transform:translate(0,0);}
    12%{opacity:.85;}
    50%{opacity:.45; transform:translate(10px,-70px);}
    88%{opacity:.7;}
    100%{opacity:0; transform:translate(-6px,-150px);}
  }
  .bg-orb{position:fixed; border-radius:50%; filter:blur(80px); opacity:.28; pointer-events:none; z-index:0;}
  .orb1{width:360px; height:360px; background:#e0a458; top:-110px; left:-90px; animation:floatOrb 34s ease-in-out infinite;}
  .orb2{width:300px; height:300px; background:#6b8f5a; bottom:-90px; right:-70px; animation:floatOrb 38s ease-in-out infinite reverse;}
  .orb3{width:240px; height:240px; background:#c9603f; top:38%; right:6%; animation:floatOrb 42s ease-in-out infinite;}
  @keyframes floatOrb{0%,100%{transform:translate(0,0);} 50%{transform:translate(16px,-20px);}}
  .wrap{width:100%; max-width:560px; position:relative; z-index:1;}
  .brand{display:flex; align-items:center; gap:.6rem; margin-bottom:.4rem; justify-content:center;}
  .brand .logo{font-size:1.9rem; filter:drop-shadow(0 4px 14px rgba(224,164,88,.7)); animation:flicker 2.6s ease-in-out infinite;}
  @keyframes flicker{0%,100%{opacity:1; transform:scale(1) rotate(0deg);} 45%{opacity:.85; transform:scale(.97) rotate(-2deg);} 55%{opacity:1; transform:scale(1.03) rotate(2deg);}}
  .brand h1{font-family:'Cinzel','Inter',serif; font-size:1.4rem; font-weight:700; margin:0; letter-spacing:.04em;}
  .tagline{text-align:center; font-size:.78rem; color:var(--text-dim); font-style:italic; margin:0 0 1.4rem; opacity:.8;}
  nav.tabs{display:flex; justify-content:center; gap:.4rem; margin-bottom:1.6rem;}
  nav.tabs a{
    color:var(--text-dim); text-decoration:none; font-weight:600; font-size:.85rem;
    padding:.45rem 1rem; border-radius:999px; transition:all .2s ease;
  }
  nav.tabs a.active{background:var(--card); color:var(--text); border:1px solid var(--card-border);}
  nav.tabs a:hover{color:var(--text);}
  .card{
    position:relative; background:var(--card); border:1px solid var(--card-border); border-radius:22px;
    padding:1.6rem; backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px);
    box-shadow:0 20px 60px rgba(0,0,0,.35); animation:cardIn .5s cubic-bezier(.2,.9,.3,1);
  }
  .card::before{
    content:''; position:absolute; inset:-2px; border-radius:24px; z-index:-1;
    background:radial-gradient(circle at 50% 40%, rgba(224,164,88,.4), transparent 70%);
    filter:blur(20px); animation:breathe 4.5s ease-in-out infinite;
  }
  @keyframes breathe{0%,100%{opacity:.35; transform:scale(1);} 50%{opacity:.6; transform:scale(1.03);}}
  @keyframes cardIn{from{opacity:0; transform:translateY(14px) scale(.98);} to{opacity:1; transform:none;}}
  .pill{
    display:inline-flex; align-items:center; gap:.35rem; font-size:.75rem; font-weight:600;
    padding:.3rem .7rem; border-radius:999px; background:rgba(255,255,255,.08);
  }
  .pill.ok{color:var(--accent2);} .pill.no{color:var(--danger);}
  .dot{width:.5rem; height:.5rem; border-radius:50%; background:currentColor;}
  .field{position:relative; margin-bottom:1rem;}
  input[type=text]{
    width:100%; padding:.95rem 3.4rem .95rem 1.1rem; font-size:1rem; font-family:inherit;
    border-radius:16px; border:1px solid var(--card-border); background:rgba(0,0,0,.25); color:var(--text);
    outline:none; transition:border-color .2s, box-shadow .2s;
  }
  input[type=text]:focus{border-color:var(--accent); box-shadow:0 0 0 3px rgba(224,164,88,.25);}
  input[type=text].flash{animation:flash .6s ease;}
  @keyframes flash{0%{box-shadow:0 0 0 0 rgba(224,164,88,.55);} 100%{box-shadow:0 0 0 10px rgba(224,164,88,0);}}
  .paste-btn{
    position:absolute; right:.45rem; top:50%; transform:translateY(-50%);
    width:2.3rem; height:2.3rem; border:none; border-radius:10px; cursor:pointer;
    background:rgba(255,255,255,.08); color:var(--text-dim); font-size:1.05rem;
    transition:background .15s, color .15s;
  }
  .paste-btn:hover{background:rgba(255,255,255,.16); color:var(--text);}
  .hint{font-size:.76rem; color:var(--warn); margin:-.5rem .2rem 1rem; opacity:0; max-height:0; overflow:hidden; transition:opacity .25s, max-height .25s;}
  .hint.show{opacity:1; max-height:3rem;}
  .toggle-row{display:flex; align-items:center; justify-content:space-between; margin:0 .1rem 1.2rem;}
  .toggle-row span{font-size:.9rem; color:var(--text-dim); font-weight:500;}
  .quality-block{margin:0 .1rem 1.2rem;}
  .quality-label{display:block; font-size:.82rem; color:var(--text-dim); font-weight:500; margin-bottom:.55rem;}
  .chip-row{display:flex; gap:.4rem; flex-wrap:wrap;}
  .chip{position:relative;}
  .chip input{position:absolute; opacity:0; width:0; height:0;}
  .chip span{
    display:inline-block; padding:.42rem .9rem; border-radius:10px; font-size:.78rem; font-weight:600;
    background:rgba(0,0,0,.2); color:var(--text-dim);
    box-shadow:inset 0 0 0 1px rgba(255,255,255,.07);
    cursor:pointer; transition:background .2s, color .2s, box-shadow .2s;
  }
  .chip span:hover{color:var(--text); box-shadow:inset 0 0 0 1px rgba(255,255,255,.18);}
  .chip input:checked + span{
    background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#241a0d; font-weight:700;
    box-shadow:0 4px 14px rgba(224,164,88,.4);
  }
  .quality-badge{
    font-size:.66rem; color:var(--text-dim); background:rgba(255,255,255,.08); font-weight:600;
    padding:.15rem .55rem; border-radius:999px; flex-shrink:0; white-space:nowrap;
  }
  .switch{position:relative; width:2.6rem; height:1.5rem; flex-shrink:0;}
  .switch input{opacity:0; width:0; height:0;}
  .track{position:absolute; inset:0; background:rgba(255,255,255,.15); border-radius:999px; transition:background .2s; cursor:pointer;}
  .track::after{content:''; position:absolute; width:1.1rem; height:1.1rem; left:.2rem; top:.2rem; background:#fff; border-radius:50%; transition:transform .2s; box-shadow:0 2px 6px rgba(0,0,0,.3);}
  .switch input:checked + .track{background:linear-gradient(135deg,var(--accent),var(--accent2));}
  .switch input:checked + .track::after{transform:translateX(1.1rem);}
  button.primary{
    width:100%; padding:.95rem; font-size:1rem; font-weight:700; font-family:inherit;
    border:none; border-radius:16px; color:#fff; cursor:pointer; position:relative; overflow:hidden;
    background:linear-gradient(135deg,var(--accent),#b9793a);
    box-shadow:0 10px 24px rgba(224,164,88,.3);
    transition:transform .15s, box-shadow .15s; display:flex; align-items:center; justify-content:center; gap:.5rem;
  }
  button.primary::after{
    content:''; position:absolute; top:0; left:-150%; width:60%; height:100%;
    background:linear-gradient(120deg, transparent, rgba(255,255,255,.35), transparent);
    transform:skewX(-20deg); transition:left .5s ease;
  }
  button.primary:hover::after{left:150%;}
  button.primary:hover{transform:translateY(-2px); box-shadow:0 14px 30px rgba(224,164,88,.4);}
  button.primary:active{transform:translateY(0) scale(.98);}
  button.primary:disabled{opacity:.7; cursor:default; transform:none;}
  .spinner{width:1rem; height:1rem; border-radius:50%; border:2px solid rgba(255,255,255,.4); border-top-color:#fff; animation:spin .7s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  #jobs{margin-top:1.4rem; display:flex; flex-direction:column; gap:.7rem;}
  .job{
    background:rgba(0,0,0,.22); border:1px solid var(--card-border); border-radius:16px; padding:.85rem 1rem;
    animation:cardIn .35s ease; transition:opacity .35s, transform .35s, max-height .35s;
  }
  .job.leaving{opacity:0; transform:scale(.96) translateY(-6px);}
  .job-top{display:flex; justify-content:space-between; align-items:center; gap:.6rem; margin-bottom:.55rem;}
  .job-url{font-size:.82rem; color:var(--text-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1;}
  .status-pill{display:flex; align-items:center; gap:.35rem; font-size:.72rem; font-weight:700; letter-spacing:.02em; flex-shrink:0;}
  .status-running{color:var(--warn);} .status-done{color:var(--accent2);} .status-error{color:var(--danger);}
  .status-running .dot{animation:pulse 1.1s ease infinite;}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}
  .bar-track{height:.5rem; border-radius:999px; background:rgba(255,255,255,.1); overflow:hidden; position:relative;}
  .bar-fill{height:100%; border-radius:999px; background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width .4s ease; width:0%;}
  .bar-fill.indet{width:40%!important; position:absolute; animation:indet 1.3s ease-in-out infinite;}
  @keyframes indet{0%{left:-40%;}100%{left:100%;}}
  .bar-fill.done{background:var(--accent2);}
  .bar-fill.error{background:var(--danger);}
  .job-meta{display:flex; justify-content:space-between; align-items:center; margin-top:.5rem; font-size:.78rem; color:var(--text-dim);}
  .job-actions{display:flex; gap:.5rem; margin-top:.65rem;}
  .btn-small{
    font-family:inherit; font-size:.8rem; font-weight:700; padding:.5rem .9rem; border-radius:10px; cursor:pointer; border:none;
    display:flex; align-items:center; gap:.4rem; transition:transform .15s;
  }
  .btn-small:active{transform:scale(.96);}
  .save-row{display:flex; gap:.5rem;}
  .btn-save{background:linear-gradient(135deg,var(--accent2),#4d6b3f); color:#fff; box-shadow:0 6px 16px rgba(107,143,90,.35); flex:1; justify-content:center;}
  .btn-save.glow{animation:glow 1.6s ease infinite;}
  @keyframes glow{0%,100%{box-shadow:0 6px 16px rgba(107,143,90,.35);}50%{box-shadow:0 6px 26px rgba(107,143,90,.7);}}
  .btn-ghost{background:rgba(255,255,255,.08); color:var(--text-dim);}
  .btn-ghost:hover{color:var(--text);}
  details{margin-top:.5rem;}
  summary{font-size:.75rem; color:var(--text-dim); cursor:pointer;}
  pre{background:rgba(0,0,0,.4); padding:.6rem; border-radius:10px; font-size:.72rem; overflow-x:auto; max-height:160px; color:#c9c9d6; margin:.4rem 0 0;}
  .empty{text-align:center; color:var(--text-dim); font-size:.85rem; padding:1.2rem 0;}
  .saved-check{font-size:1.4rem; animation:pop .4s ease;}
  @keyframes pop{0%{transform:scale(0);}70%{transform:scale(1.2);}100%{transform:scale(1);}}
  .foot{text-align:center; font-size:.72rem; color:var(--text-dim); margin-top:1.2rem; opacity:.7;}
  .dropzone{
    border:2px dashed var(--card-border); border-radius:16px; padding:1.6rem 1rem; text-align:center;
    color:var(--text-dim); font-size:.88rem; cursor:pointer; transition:all .2s; margin-bottom:1rem;
  }
  .dropzone.drag{border-color:var(--accent); background:rgba(224,164,88,.08); color:var(--text);}
  .dropzone input{display:none;}
  a.linklike{color:var(--accent); font-weight:600;}
</style>
"""

PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ page_title }}</title>
""" + BASE_STYLE + """
</head>
<body>
"""

INDEX_HTML = PAGE_HEAD + """
<div class="fireflies">
  <span class="firefly" style="left:8%;top:82%;animation-duration:9s;animation-delay:0s;"></span>
  <span class="firefly" style="left:22%;top:66%;animation-duration:11s;animation-delay:2s;"></span>
  <span class="firefly" style="left:75%;top:76%;animation-duration:8s;animation-delay:4s;"></span>
  <span class="firefly" style="left:60%;top:42%;animation-duration:12s;animation-delay:1s;"></span>
  <span class="firefly" style="left:40%;top:86%;animation-duration:10s;animation-delay:3s;"></span>
  <span class="firefly" style="left:88%;top:56%;animation-duration:9.5s;animation-delay:5s;"></span>
  <span class="firefly" style="left:15%;top:32%;animation-duration:10.5s;animation-delay:6s;"></span>
  <span class="firefly" style="left:50%;top:22%;animation-duration:11.5s;animation-delay:2.5s;"></span>
</div>
<div class="bg-orb orb1"></div>
<div class="bg-orb orb2"></div>
<div class="bg-orb orb3"></div>
<div class="wrap">
  <div class="brand"><span class="logo">🕯️</span><h1>Grab</h1></div>
  <p class="tagline">curl up, paste a link, let the fire do the rest ✨</p>
  <nav class="tabs"><a href="/" class="active">Download</a><a href="/settings">Settings</a></nav>
  <div class="card">
    <form id="dlform">
      <div class="field">
        <input type="text" id="url" placeholder="Paste a video link…" inputmode="url" autocomplete="off"
               autocapitalize="off" autocorrect="off" spellcheck="false" autofocus required>
        <button type="button" class="paste-btn" id="pasteBtn" title="Paste from clipboard" aria-label="Paste from clipboard">📋</button>
      </div>
      <div class="hint" id="pasteHint">One-tap paste is blocked on plain http — long-press the field and tap Paste.</div>
      <div class="toggle-row">
        <span>🎵 Audio only (MP3)</span>
        <label class="switch"><input type="checkbox" id="audioToggle"><span class="track"></span></label>
      </div>
      <div class="quality-block" id="videoQualityBlock">
        <span class="quality-label">🎚️ Quality</span>
        <div class="chip-row">
          <label class="chip"><input type="radio" name="vq" value="best" checked><span>Best</span></label>
          <label class="chip"><input type="radio" name="vq" value="2160"><span>4K</span></label>
          <label class="chip"><input type="radio" name="vq" value="1440"><span>1440p</span></label>
          <label class="chip"><input type="radio" name="vq" value="1080"><span>1080p</span></label>
          <label class="chip"><input type="radio" name="vq" value="720"><span>720p</span></label>
          <label class="chip"><input type="radio" name="vq" value="480"><span>480p</span></label>
          <label class="chip"><input type="radio" name="vq" value="360"><span>360p</span></label>
        </div>
        <div class="toggle-row" style="margin:1rem 0 0;">
          <span>📱 iPhone-friendly (H.264) <small style="opacity:.7">· max 1080p</small></span>
          <label class="switch"><input type="checkbox" id="iosToggle" checked><span class="track"></span></label>
        </div>
      </div>
      <div class="quality-block" id="audioQualityBlock" style="display:none;">
        <span class="quality-label">🎚️ Bitrate</span>
        <div class="chip-row">
          <label class="chip"><input type="radio" name="aq" value="default" checked><span>Best</span></label>
          <label class="chip"><input type="radio" name="aq" value="320"><span>320k</span></label>
          <label class="chip"><input type="radio" name="aq" value="256"><span>256k</span></label>
          <label class="chip"><input type="radio" name="aq" value="192"><span>192k</span></label>
          <label class="chip"><input type="radio" name="aq" value="128"><span>128k</span></label>
        </div>
      </div>
      <div class="toggle-row">
        <span>🖼️ Embed thumbnail</span>
        <label class="switch"><input type="checkbox" id="embedToggle"><span class="track"></span></label>
      </div>
      <div class="toggle-row">
        <span>🗂️ Save thumbnail as a file</span>
        <label class="switch"><input type="checkbox" id="saveThumbToggle"><span class="track"></span></label>
      </div>
      <button type="submit" class="primary" id="submitBtn"><span id="submitLabel">✨ Download</span></button>
    </form>
    <div style="margin-top:1rem; text-align:center;">
      <span class="pill {{ 'ok' if cookie_ok else 'no' }}"><span class="dot"></span>
        {{ 'Cookies loaded' if cookie_ok else 'No cookies — some videos may fail' }}</span>
    </div>
    <div id="jobs"></div>
  </div>
</div>
<script>
const jobsEl = document.getElementById('jobs');
const cards = new Map();

function fmtSize(b){
  if(!b) return '';
  const u=['B','KB','MB','GB']; let i=0;
  while(b>=1024 && i<u.length-1){b/=1024;i++;}
  return b.toFixed(1)+u[i];
}

function createCard(job){
  const el = document.createElement('div');
  el.className = 'job';
  el.innerHTML = `
    <div class="job-top">
      <div class="job-url" title="${job.url}">${job.url}</div>
      <span class="quality-badge">${job.quality_label || ''}</span>
      <div class="status-pill status-${job.status}"><span class="dot"></span><span class="stext"></span></div>
    </div>
    <div class="bar-track"><div class="bar-fill"></div></div>
    <div class="job-meta"><span class="phase"></span><span class="pct"></span></div>
    <div class="actions"></div>
  `;
  jobsEl.prepend(el);
  return {
    el,
    status: el.querySelector('.status-pill'),
    stext: el.querySelector('.stext'),
    bar: el.querySelector('.bar-fill'),
    phase: el.querySelector('.phase'),
    pct: el.querySelector('.pct'),
    actions: el.querySelector('.actions'),
    wiredError: false, wiredCancel: false,
    doneInit: false, videoNeeded: false, thumbNeeded: false,
    videoState: 'idle', thumbState: 'idle',
  };
}

function updateCard(c, job){
  c.status.className = 'status-pill status-' + job.status;
  c.stext.textContent = job.status === 'running' ? 'Running' : (job.status === 'done' ? 'Ready' : 'Failed');
  c.phase.textContent = job.phase;
  if (job.status === 'running' && !c.wiredCancel) {
    c.wiredCancel = true;
    const btn = document.createElement('button');
    btn.className = 'btn-small btn-ghost';
    btn.textContent = '✕ Cancel';
    btn.onclick = async () => {
      btn.disabled = true;
      await fetch('/jobs/' + job.id, {method: 'DELETE'});
      removeCard(job.id);
    };
    c.actions.innerHTML = '';
    c.actions.appendChild(btn);
  }
  if (job.status === 'error') {
    c.bar.className = 'bar-fill error'; c.bar.style.width = '100%';
    c.pct.textContent = '';
    if (!c.wiredError) {
      c.wiredError = true;
      c.actions.innerHTML = `<button class="btn-small btn-save" style="background:linear-gradient(135deg,#c9603f,#8a3a26)">↻ Retry</button>
        <button class="btn-small btn-ghost">✕ Dismiss</button>
        <details><summary>Show details</summary><pre></pre></details>`;
      c.actions.querySelector('pre').textContent = job.tail || '(no output)';
      c.actions.querySelectorAll('.btn-small')[0].onclick = () => {
        submitUrl(job.url, currentOptions());
        removeCard(job.id);
      };
      c.actions.querySelectorAll('.btn-small')[1].onclick = () => { fetch('/jobs/'+job.id, {method:'DELETE'}); removeCard(job.id); };
    }
    return;
  }
  if (job.percent != null) {
    c.bar.className = 'bar-fill' + (job.status === 'done' ? ' done' : '');
    c.bar.style.width = job.percent + '%';
    c.pct.textContent = job.status === 'done' ? '' : Math.round(job.percent) + '%';
  } else {
    c.bar.className = 'bar-fill indet';
    c.pct.textContent = '';
  }
  if (job.status === 'done' && !c.doneInit) {
    c.doneInit = true;
    c.videoNeeded = !!job.filename;
    c.thumbNeeded = !!job.thumb_filename;
    const row = document.createElement('div');
    row.className = 'save-row';
    const mediaLabel = job.kind === 'audio' ? '🎵 Save audio (MP3)' : '🎬 Save video';
    if (c.videoNeeded) row.appendChild(makeSaveButton(c, job.id, 'video', mediaLabel + (job.size ? ' · '+fmtSize(job.size) : '')));
    if (c.thumbNeeded) row.appendChild(makeSaveButton(c, job.id, 'thumb', '🖼️ Save thumbnail' + (job.thumb_size ? ' · '+fmtSize(job.thumb_size) : '')));
    c.actions.innerHTML = '';
    c.actions.appendChild(row);
  }
}

function makeSaveButton(c, jobId, kind, label){
  const btn = document.createElement('button');
  btn.className = 'btn-small btn-save glow';
  btn.textContent = label;
  btn.onclick = () => downloadKind(c, jobId, kind, btn, label);
  return btn;
}

async function downloadKind(c, jobId, kind, btn, origLabel){
  btn.disabled = true;
  btn.classList.remove('glow');
  const endpoint = kind === 'video' ? `/files/${jobId}` : `/files/${jobId}/thumbnail`;
  try {
    const res = await fetch(endpoint);
    if (!res.ok) throw new Error('expired');
    const total = parseInt(res.headers.get('Content-Length') || '0', 10);
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    const filename = m ? m[1] : (kind === 'video' ? 'video.mp4' : 'thumbnail.jpg');
    if (res.body && res.body.getReader) {
      const reader = res.body.getReader();
      const chunks = []; let received = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        chunks.push(value); received += value.length;
        if (total) btn.textContent = Math.round(received / total * 100) + '%';
      }
      const blob = new Blob(chunks);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 15000);
    } else {
      window.location = endpoint;
    }
    btn.textContent = '✅ Saved';
    if (kind === 'video') c.videoState = 'done'; else c.thumbState = 'done';
    const videoOk = !c.videoNeeded || c.videoState === 'done';
    const thumbOk = !c.thumbNeeded || c.thumbState === 'done';
    if (videoOk && thumbOk) setTimeout(() => removeCard(jobId), 2500);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = origLabel;
    btn.classList.add('glow');
  }
}

function removeCard(id){
  const c = cards.get(id);
  if (!c) return;
  c.el.classList.add('leaving');
  setTimeout(() => { c.el.remove(); cards.delete(id); renderEmpty(); }, 350);
}

function renderEmpty(){
  if (cards.size === 0 && !jobsEl.querySelector('.empty')) {
    const p = document.createElement('div'); p.className = 'empty'; p.textContent = 'No downloads yet — paste a link above.';
    jobsEl.appendChild(p);
  }
}

async function poll(){
  try {
    const res = await fetch('/jobs.json');
    const jobs = await res.json();
    const seen = new Set();
    for (const job of jobs) {
      seen.add(job.id);
      let c = cards.get(job.id);
      if (!c) {
        jobsEl.querySelector('.empty')?.remove();
        c = createCard(job);
        cards.set(job.id, c);
      }
      updateCard(c, job);
    }
    for (const [id, c] of cards) {
      if (!seen.has(id)) removeCard(id);
    }
    renderEmpty();
  } catch (e) {}
}
renderEmpty();
setInterval(poll, 1300);
poll();

const flag = (id) => document.getElementById(id).checked ? '1' : '0';

function currentOptions(){
  return {
    audio_only: flag('audioToggle'),
    embed_thumbnail: flag('embedToggle'),
    save_thumbnail: flag('saveThumbToggle'),
    ios_compat: flag('iosToggle'),
    video_quality: document.querySelector('input[name="vq"]:checked').value,
    audio_quality: document.querySelector('input[name="aq"]:checked').value,
  };
}

async function submitUrl(url, options){
  await fetch('/download', {method:'POST', body: new URLSearchParams({url, ...options})});
  poll();
}

const pasteHint = document.getElementById('pasteHint');
document.getElementById('pasteBtn').addEventListener('click', async () => {
  const field = document.getElementById('url');
  field.focus();
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.readText) {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text) {
        field.value = text;
        field.classList.remove('flash'); void field.offsetWidth; field.classList.add('flash');
        pasteHint.classList.remove('show');
        return;
      }
    } catch (e) { /* permission denied — fall through to the hint */ }
  }
  pasteHint.classList.add('show');
  setTimeout(() => pasteHint.classList.remove('show'), 6000);
});

const form = document.getElementById('dlform');
const input = document.getElementById('url');
const submitBtn = document.getElementById('submitBtn');
const submitLabel = document.getElementById('submitLabel');
const audioToggle = document.getElementById('audioToggle');
const videoQualityBlock = document.getElementById('videoQualityBlock');
const audioQualityBlock = document.getElementById('audioQualityBlock');

audioToggle.addEventListener('change', () => {
  const isAudio = audioToggle.checked;
  videoQualityBlock.style.display = isAudio ? 'none' : '';
  audioQualityBlock.style.display = isAudio ? '' : 'none';
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = input.value.trim();
  if (!url) return;
  submitBtn.disabled = true;
  submitLabel.innerHTML = '<span class="spinner"></span>';
  try {
    await submitUrl(url, currentOptions());
    input.value = '';
  } finally {
    submitBtn.disabled = false;
    submitLabel.textContent = '✨ Download';
    input.focus();
  }
});
</script>
</body>
</html>
"""

SETTINGS_HTML = PAGE_HEAD + """
<div class="fireflies">
  <span class="firefly" style="left:8%;top:82%;animation-duration:9s;animation-delay:0s;"></span>
  <span class="firefly" style="left:22%;top:66%;animation-duration:11s;animation-delay:2s;"></span>
  <span class="firefly" style="left:75%;top:76%;animation-duration:8s;animation-delay:4s;"></span>
  <span class="firefly" style="left:60%;top:42%;animation-duration:12s;animation-delay:1s;"></span>
  <span class="firefly" style="left:40%;top:86%;animation-duration:10s;animation-delay:3s;"></span>
  <span class="firefly" style="left:88%;top:56%;animation-duration:9.5s;animation-delay:5s;"></span>
  <span class="firefly" style="left:15%;top:32%;animation-duration:10.5s;animation-delay:6s;"></span>
  <span class="firefly" style="left:50%;top:22%;animation-duration:11.5s;animation-delay:2.5s;"></span>
</div>
<div class="bg-orb orb1"></div>
<div class="bg-orb orb2"></div>
<div class="bg-orb orb3"></div>
<div class="wrap">
  <div class="brand"><span class="logo">🕯️</span><h1>Grab</h1></div>
  <p class="tagline">your cookies, kept safe by the hearth ✨</p>
  <nav class="tabs"><a href="/">Download</a><a href="/settings" class="active">Settings</a></nav>
  <div class="card">
    <p style="margin-top:0; color:var(--text-dim); font-size:.9rem;">
      Cookies are used to download age-restricted or members-only videos as yourself.
    </p>
    <p><span class="pill {{ 'ok' if cookie_ok else 'no' }}"><span class="dot"></span>
      {{ 'cookies.txt loaded' if cookie_ok else 'No cookies.txt uploaded' }}</span></p>
    <form id="cform" method="post" action="/settings/cookies" enctype="multipart/form-data">
      <div class="dropzone" id="dz">
        📄 Drop <b>cookies.txt</b> here, or click to choose a file
        <input type="file" name="cookies" id="cfile" accept=".txt" required>
      </div>
      <button type="submit" class="primary">Upload cookies.txt</button>
    </form>
    {% if cookie_ok %}
    <button class="btn-small btn-ghost" id="clearBtn" style="margin-top:.8rem; width:100%; justify-content:center;">✕ Remove cookies</button>
    {% endif %}
  </div>
  <div class="foot">Exported from your browser via a cookies.txt extension (e.g. "Get cookies.txt LOCALLY").</div>
</div>
<script>
const dz = document.getElementById('dz');
const cfile = document.getElementById('cfile');
dz.addEventListener('click', () => cfile.click());
['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('drag'); }));
['dragleave','drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('drag'); }));
dz.addEventListener('drop', e => {
  if (e.dataTransfer.files.length) {
    cfile.files = e.dataTransfer.files;
    document.getElementById('cform').requestSubmit();
  }
});
cfile.addEventListener('change', () => { if (cfile.files.length) document.getElementById('cform').requestSubmit(); });
document.getElementById('clearBtn')?.addEventListener('click', async () => {
  await fetch('/settings/cookies', {method:'DELETE'});
  location.reload();
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, threaded=True)
