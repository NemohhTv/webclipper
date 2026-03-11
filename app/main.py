import os
import json
import subprocess
import time
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

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": True,
    "refresh_interval": 20,
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v"}


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
    sources = []
    for s in config.get("sources", []):
        count = 0
        if os.path.isdir(s["path"]):
            for root, dirs, files in os.walk(s["path"]):
                count += sum(1 for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS)
        sources.append({"label": s["label"], "path": s["path"], "count": count})
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
    return {"status": "ok"}


@app.delete("/api/sources/{label}")
async def remove_source(label: str):
    config = load_config()
    config["sources"] = [s for s in config.get("sources", []) if s["label"] != label]
    save_config(config)
    return {"status": "ok"}


# ── Recordings ────────────────────────────────────────────────
@app.get("/api/recordings")
async def get_recordings(source: Optional[str] = None):
    config = load_config()
    recordings = []
    sources_to_scan = config.get("sources", [])
    if source:
        sources_to_scan = [s for s in sources_to_scan if s["label"] == source]

    for s in sources_to_scan:
        if not os.path.isdir(s["path"]):
            continue
        for root, dirs, files in os.walk(s["path"]):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    filepath = os.path.join(root, f)
                    stat = os.stat(filepath)
                    recordings.append({
                        "name": f,
                        "path": filepath,
                        "source": s["label"],
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })

    recordings.sort(key=lambda r: r["modified"], reverse=True)
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

    return {"audio_tracks": audio_tracks, "path": path}


# ── Streaming ─────────────────────────────────────────────────
@app.get("/stream")
async def stream_video(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4")


@app.get("/stream/clip")
async def stream_clip(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4")


# ── Clips ─────────────────────────────────────────────────────
@app.post("/api/clip")
async def create_clip(req: ClipRequest):
    if not os.path.isfile(req.filepath):
        raise HTTPException(404, "Source file not found")

    os.makedirs(CLIPS_DIR, exist_ok=True)

    # Sanitize title for filename
    safe_title = "".join(c for c in req.title if c.isalnum() or c in " -_").strip()
    if not safe_title:
        safe_title = "clip"
    timestamp = int(time.time())
    out_filename = f"{safe_title}_{timestamp}.mp4"
    out_path = os.path.join(CLIPS_DIR, out_filename)

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
    os.makedirs(CLIPS_DIR, exist_ok=True)
    clips = []
    for f in os.listdir(CLIPS_DIR):
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            filepath = os.path.join(CLIPS_DIR, f)
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
    if not os.path.isfile(path):
        raise HTTPException(404, "Clip not found")
    if not path.startswith(CLIPS_DIR):
        raise HTTPException(403, "Cannot delete files outside clips directory")
    os.remove(path)
    # Remove metadata too
    meta_path = path + ".json"
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
    return load_config()


@app.put("/api/settings")
async def update_settings(settings: SettingsModel):
    config = load_config()
    config["auto_refresh"] = settings.auto_refresh
    config["refresh_interval"] = settings.refresh_interval
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
