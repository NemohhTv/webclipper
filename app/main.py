import os
import json
import mimetypes
import subprocess
import time
import uuid
import threading
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="WebClipper")

# ── Config ────────────────────────────────────────────────────
CONFIG_DIR = "/app/data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CLIPS_DIR = os.path.join(CONFIG_DIR, "clips")
PREVIEW_DIR = os.path.join(CONFIG_DIR, "preview")

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": True,
    "refresh_interval": 20,
    "clips_path": CLIPS_DIR,
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v"}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}

SCAN_CACHE_TTL = 10
_RECORDINGS_CACHE = {"timestamp": 0.0, "data": []}
_REMUX_JOBS = {}
_REMUX_JOBS_LOCK = threading.Lock()


def _is_video_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


def _scan_recordings(sources):
    now = time.time()
    if (now - _RECORDINGS_CACHE["timestamp"]) < SCAN_CACHE_TTL:
        return _RECORDINGS_CACHE["data"]

    recordings = []
    for source in sources:
        source_path = source.get("path", "")
        if not os.path.isdir(source_path):
            continue

        for root, _, files in os.walk(source_path):
            for filename in files:
                if not _is_video_file(filename):
                    continue

                filepath = os.path.join(root, filename)
                try:
                    stat = os.stat(filepath)
                except OSError:
                    continue

                recordings.append({
                    "name": filename,
                    "path": filepath,
                    "source": source["label"],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })

    recordings.sort(key=lambda item: item["modified"], reverse=True)
    _RECORDINGS_CACHE["timestamp"] = now
    _RECORDINGS_CACHE["data"] = recordings
    return recordings


def _invalidate_recordings_cache():
    _RECORDINGS_CACHE["timestamp"] = 0.0
    _RECORDINGS_CACHE["data"] = []


def _mime_type_for_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _get_clips_dir(config=None) -> str:
    cfg = config or load_config()
    clips_path = cfg.get("clips_path") or CLIPS_DIR
    resolved = os.path.abspath(clips_path)
    os.makedirs(resolved, exist_ok=True)
    return resolved


def _update_remux_job(job_id: str, **updates):
    with _REMUX_JOBS_LOCK:
        job = _REMUX_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def _run_remux_job(job_id: str):
    with _REMUX_JOBS_LOCK:
        job = _REMUX_JOBS.get(job_id)
    if not job:
        return

    source_path = job["source_path"]
    output_path = job["output_path"]

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", source_path,
        "-map", "0:v", "-map", "0:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]

    _update_remux_job(job_id, status="running", started_at=int(time.time()))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not os.path.exists(output_path):
            detail = (result.stderr or "")[-500:]
            _update_remux_job(job_id, status="failed", error=f"Remux failed. {detail}", finished_at=int(time.time()))
            return

        try:
            os.remove(source_path)
        except OSError as exc:
            _update_remux_job(
                job_id,
                status="failed",
                error=f"Remux succeeded but failed to delete source file: {exc}",
                finished_at=int(time.time()),
            )
            return

        stat = os.stat(output_path)
        _update_remux_job(
            job_id,
            status="completed",
            finished_at=int(time.time()),
            result={
                "name": os.path.basename(output_path),
                "path": output_path,
                "size": stat.st_size,
                "deleted_source": True,
            },
        )
        _invalidate_recordings_cache()
    except Exception as exc:
        _update_remux_job(job_id, status="failed", error=f"Remux failed: {exc}", finished_at=int(time.time()))


def _preview_output_path(path: str) -> str:
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    stat = os.stat(path)
    cache_key = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
    import hashlib
    filename = hashlib.md5(cache_key.encode()).hexdigest() + ".mp4"
    return os.path.join(PREVIEW_DIR, filename)


def _build_preview_mp4(path: str) -> str:
    out_path = _preview_output_path(path)
    if os.path.exists(out_path):
        return out_path

    # First try remux (fast, minimal CPU)
    remux_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+faststart",
        out_path,
    ]
    remux_result = subprocess.run(remux_cmd, capture_output=True, text=True, timeout=600)
    if remux_result.returncode == 0 and os.path.exists(out_path):
        return out_path

    # Fallback to transcode for incompatible codecs
    transcode_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_path,
    ]
    transcode_result = subprocess.run(transcode_cmd, capture_output=True, text=True, timeout=3600)
    if transcode_result.returncode != 0 or not os.path.exists(out_path):
        detail = (transcode_result.stderr or remux_result.stderr or "")[-500:]
        raise HTTPException(500, f"Preview conversion failed: {detail}")

    return out_path


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Models ────────────────────────────────────────────────────
class SourceModel(BaseModel):
    label: str
    path: str


class SettingsModel(BaseModel):
    auto_refresh: bool
    refresh_interval: int
    clips_path: str


class ClipRequest(BaseModel):
    filepath: str
    source_label: str
    start: float
    end: float
    title: str
    game: str = "Unknown"
    lossless: bool = True
    audio_tracks: Optional[list] = None


# ── Templates ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(template_path, "r") as f:
        return f.read()


# ── Sources ───────────────────────────────────────────────────
@app.get("/api/sources")
async def get_sources():
    config = load_config()
    recordings = _scan_recordings(config.get("sources", []))
    counts = {}
    for recording in recordings:
        label = recording["source"]
        counts[label] = counts.get(label, 0) + 1

    sources = []
    for s in config.get("sources", []):
        sources.append({"label": s["label"], "path": s["path"], "count": counts.get(s["label"], 0)})
    return {"sources": sources}


@app.post("/api/sources")
async def add_source(source: SourceModel):
    config = load_config()
    for s in config.get("sources", []):
        if s["label"] == source.label:
            raise HTTPException(400, f'Source "{source.label}" already exists')
    if not os.path.isdir(source.path):
        raise HTTPException(400, f"Directory not found: {source.path}")
    config.setdefault("sources", []).append({"label": source.label, "path": source.path})
    save_config(config)
    _invalidate_recordings_cache()
    return {"status": "ok"}


@app.delete("/api/sources/{label}")
async def remove_source(label: str):
    config = load_config()
    config["sources"] = [s for s in config.get("sources", []) if s["label"] != label]
    save_config(config)
    _invalidate_recordings_cache()
    return {"status": "ok"}


# ── Recordings ────────────────────────────────────────────────
@app.get("/api/recordings")
async def get_recordings(source: Optional[str] = None):
    config = load_config()
    recordings = _scan_recordings(config.get("sources", []))
    if source:
        recordings = [item for item in recordings if item["source"] == source]
    return {"recordings": recordings}


@app.get("/api/recordings/thumbnail")
async def get_thumbnail(path: str, time: float = 1):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")

    thumb_dir = os.path.join(CONFIG_DIR, "thumbnails")
    os.makedirs(thumb_dir, exist_ok=True)

    # Use a hash of the path for the thumbnail filename
    import hashlib
    path_hash = hashlib.md5(path.encode()).hexdigest()
    thumb_path = os.path.join(thumb_dir, f"{path_hash}.jpg")

    if not os.path.exists(thumb_path):
        try:
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(time), "-i", path,
                "-vframes", "1", "-q:v", "8",
                "-vf", "scale=480:-1",
                thumb_path
            ], capture_output=True, timeout=15)
        except Exception:
            raise HTTPException(500, "Failed to generate thumbnail")

    if not os.path.exists(thumb_path):
        raise HTTPException(500, "Thumbnail generation failed")

    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/api/recordings/info")
async def get_recording_info(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")

    audio_tracks = []
    try:
        result = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", path
        ], capture_output=True, text=True, timeout=10)
        probe = json.loads(result.stdout)
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "audio":
                audio_tracks.append({
                    "index": stream.get("index", 0),
                    "language": stream.get("tags", {}).get("language", "und"),
                    "codec": stream.get("codec_name", "unknown"),
                    "channels": stream.get("channels", 2),
                })
    except Exception:
        pass

    extension = os.path.splitext(path)[1].lower()
    return {
        "audio_tracks": audio_tracks,
        "path": path,
        "extension": extension,
        "needs_transcode_preview": extension not in DIRECT_PLAY_EXTENSIONS,
    }


# ── Streaming ─────────────────────────────────────────────────
@app.get("/stream")
async def stream_video(path: str, transcode: bool = False):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")

    extension = os.path.splitext(path)[1].lower()
    if transcode or extension not in DIRECT_PLAY_EXTENSIONS:
        preview_path = _build_preview_mp4(path)
        return FileResponse(preview_path, media_type="video/mp4")

    return FileResponse(path, media_type=_mime_type_for_path(path))


@app.post("/api/recordings/remux")
async def remux_recording(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")

    base_name = os.path.splitext(os.path.basename(path))[0]
    output_name = f"{base_name}_remux_{int(time.time())}.mp4"
    output_path = os.path.join(os.path.dirname(path), output_name)
    job_id = uuid.uuid4().hex

    with _REMUX_JOBS_LOCK:
        _REMUX_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "source_path": path,
            "output_path": output_path,
            "created_at": int(time.time()),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
        }

    worker = threading.Thread(target=_run_remux_job, args=(job_id,), daemon=True)
    worker.start()

    return {
        "status": "queued",
        "job_id": job_id,
        "source_path": path,
        "output_path": output_path,
    }


@app.get("/api/recordings/remux/status")
async def remux_status(job_id: str):
    with _REMUX_JOBS_LOCK:
        job = _REMUX_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Remux job not found")
        return job


@app.get("/stream/clip")
async def stream_clip(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type=_mime_type_for_path(path))


# ── Clips ─────────────────────────────────────────────────────
@app.post("/api/clip")
async def create_clip(req: ClipRequest):
    if not os.path.isfile(req.filepath):
        raise HTTPException(404, "Source file not found")

    clips_dir = _get_clips_dir()

    # Sanitize title for filename
    safe_title = "".join(c for c in req.title if c.isalnum() or c in " -_").strip()
    if not safe_title:
        safe_title = "clip"
    timestamp = int(time.time())
    out_filename = f"{safe_title}_{timestamp}.mp4"
    out_path = os.path.join(clips_dir, out_filename)

    duration = req.end - req.start

    if req.lossless:
        # Stream copy (fast, no re-encode)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(req.start),
            "-i", req.filepath,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            out_path
        ]
    else:
        # Re-encode with optional audio mixing
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(req.start),
            "-i", req.filepath,
            "-t", str(duration),
        ]

        if req.audio_tracks:
            enabled = [t for t in req.audio_tracks if t.get("enabled", True)]
            if len(enabled) > 1:
                # Build amix filter for multiple tracks
                filter_parts = []
                for t in enabled:
                    vol = t.get("volume", 100) / 100.0
                    idx = t["index"]
                    filter_parts.append(f"[0:{idx}]volume={vol}[a{idx}]")
                mix_inputs = "".join(f"[a{t['index']}]" for t in enabled)
                filter_parts.append(f"{mix_inputs}amix=inputs={len(enabled)}:duration=first[aout]")
                cmd += ["-filter_complex", ";".join(filter_parts), "-map", "0:v", "-map", "[aout]"]
            elif len(enabled) == 1:
                vol = enabled[0].get("volume", 100) / 100.0
                idx = enabled[0]["index"]
                cmd += ["-map", "0:v", "-map", f"0:{idx}"]
                if vol != 1.0:
                    cmd += ["-af", f"volume={vol}"]
            else:
                cmd += ["-an"]  # No audio
        else:
            pass  # Default: include all streams

        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-c:a", "aac", "-b:a", "192k"]
        cmd.append(out_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise HTTPException(500, f"FFmpeg error: {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "Clip creation timed out")

    if not os.path.exists(out_path):
        raise HTTPException(500, "Output file was not created")

    # Save clip metadata
    meta = {
        "title": req.title,
        "game": req.game,
        "source": req.source_label,
        "start": req.start,
        "end": req.end,
        "lossless": req.lossless,
        "created": timestamp,
    }
    meta_path = out_path + ".json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    stat = os.stat(out_path)
    return {"name": out_filename, "path": out_path, "size": stat.st_size}


@app.get("/api/clips")
async def get_clips():
    clips_dir = _get_clips_dir()
    clips = []
    for f in os.listdir(clips_dir):
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            filepath = os.path.join(clips_dir, f)
            stat = os.stat(filepath)

            # Load metadata if exists
            meta = None
            meta_path = filepath + ".json"
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r") as mf:
                        meta = json.load(mf)
                except Exception:
                    pass

            clips.append({
                "name": f,
                "path": filepath,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "meta": meta,
            })

    clips.sort(key=lambda c: c["modified"], reverse=True)
    return {"clips": clips}


@app.delete("/api/clips")
async def delete_clip(path: str):
    clips_dir = _get_clips_dir()
    path_real = os.path.realpath(path)
    clips_real = os.path.realpath(clips_dir)
    if not os.path.isfile(path):
        raise HTTPException(404, "Clip not found")
    if not path_real.startswith(clips_real + os.sep):
        raise HTTPException(403, "Cannot delete files outside clips directory")
    os.remove(path_real)
    # Remove metadata too
    meta_path = path_real + ".json"
    if os.path.exists(meta_path):
        os.remove(meta_path)
    return {"status": "ok"}


@app.get("/api/clips/download")
async def download_clip(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "Clip not found")
    filename = os.path.basename(path)
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


# ── Settings ──────────────────────────────────────────────────
@app.get("/api/settings")
async def get_settings():
    config = load_config()
    config.setdefault("clips_path", CLIPS_DIR)
    return config


@app.put("/api/settings")
async def update_settings(settings: SettingsModel):
    config = load_config()
    config["auto_refresh"] = settings.auto_refresh
    config["refresh_interval"] = settings.refresh_interval
    clips_path = (settings.clips_path or "").strip() or CLIPS_DIR
    try:
        os.makedirs(clips_path, exist_ok=True)
    except OSError:
        raise HTTPException(400, f"Cannot use clips folder: {clips_path}")
    config["clips_path"] = os.path.abspath(clips_path)
    save_config(config)
    return {"status": "ok"}


# ── Folder Browser ────────────────────────────────────────────
@app.get("/api/browse")
async def browse_directory(path: str = "/"):
    target = path if path else "/"
    if not os.path.isdir(target):
        raise HTTPException(404, "Directory not found")
    dirs = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    return {"path": target, "parent": os.path.dirname(target), "directories": dirs}
