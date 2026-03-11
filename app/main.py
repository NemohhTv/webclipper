import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field


app = FastAPI(title="WebClipper")


# -----------------------------------------------------------------------------
# Paths / Config
# -----------------------------------------------------------------------------

DATA_DIR = os.getenv("WEBCLIPPER_DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DEFAULT_CLIPS_DIR = os.path.join(DATA_DIR, "clips")
THUMBNAILS_DIR = os.path.join(DATA_DIR, "thumbnails")
PREVIEW_DIR = os.path.join(DATA_DIR, "preview")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v"}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov"}

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": True,
    "refresh_interval": 20,
    "clips_path": DEFAULT_CLIPS_DIR,
}


def ensure_runtime_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DEFAULT_CLIPS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    os.makedirs(PREVIEW_DIR, exist_ok=True)


ensure_runtime_dirs()


def load_config() -> dict:
    ensure_runtime_dirs()
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()

    merged = DEFAULT_CONFIG.copy()
    merged.update(config or {})
    merged["sources"] = merged.get("sources", [])
    merged["clips_path"] = os.path.abspath(merged.get("clips_path") or DEFAULT_CLIPS_DIR)
    return merged


def save_config(config: dict) -> None:
    ensure_runtime_dirs()
    fd, tmp_path = tempfile.mkstemp(prefix="config_", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def get_clips_dir() -> str:
    config = load_config()
    clips_dir = os.path.abspath(config.get("clips_path") or DEFAULT_CLIPS_DIR)
    os.makedirs(clips_dir, exist_ok=True)
    return clips_dir


def sanitize_label(label: str) -> str:
    return " ".join((label or "").strip().split())


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def mime_type_for_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def is_within_allowed_roots(path: str, roots: List[str]) -> bool:
    real_path = os.path.realpath(path)
    for root in roots:
        real_root = os.path.realpath(root)
        if real_path == real_root or real_path.startswith(real_root + os.sep):
            return True
    return False


def run_ffprobe_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0.0
        return float((result.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def scan_recordings(sources: List[dict]) -> List[dict]:
    recordings: List[dict] = []

    for source in sources:
        label = source.get("label", "").strip()
        root = source.get("path", "").strip()

        if not label or not root or not os.path.isdir(root):
            continue

        try:
            for entry in os.scandir(root):
                if not entry.is_file():
                    continue
                if not is_video_file(entry.path):
                    continue

                stat = entry.stat()
                recordings.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "source": label,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
        except PermissionError:
            continue

    recordings.sort(key=lambda x: x["modified"], reverse=True)
    return recordings


def build_preview_mp4(path: str) -> str:
    real_path = os.path.realpath(path)
    file_hash = hashlib.md5(real_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(PREVIEW_DIR, f"{file_hash}.mp4")

    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(real_path):
        return out_path

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        real_path,
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail="Preview generation failed")

    return out_path


def build_thumbnail(path: str, second: float = 1.0) -> str:
    real_path = os.path.realpath(path)
    file_hash = hashlib.md5(real_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(THUMBNAILS_DIR, f"{file_hash}.jpg")

    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(real_path):
        return out_path

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(second),
        "-i",
        real_path,
        "-frames:v",
        "1",
        "-q:v",
        "5",
        "-vf",
        "scale=480:-2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail="Thumbnail generation failed")

    return out_path


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class SourceModel(BaseModel):
    label: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class SettingsModel(BaseModel):
    auto_refresh: bool
    refresh_interval: int = Field(..., ge=5, le=3600)
    clips_path: str


class ClipRequest(BaseModel):
    filepath: str
    source_label: str
    start: float
    end: float
    title: str
    game: str = "Unknown"
    lossless: bool = True


class DeleteRecordingsRequest(BaseModel):
    paths: List[str]


class DeleteClipsRequest(BaseModel):
    paths: List[str]


# -----------------------------------------------------------------------------
# Error Handling
# -----------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": str(exc)},
    )


# -----------------------------------------------------------------------------
# Frontend
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


# -----------------------------------------------------------------------------
# Sources
# -----------------------------------------------------------------------------

@app.get("/api/sources")
async def get_sources():
    config = load_config()
    recordings = scan_recordings(config.get("sources", []))
    counts = {}

    for item in recordings:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    sources = []
    for source in config.get("sources", []):
        label = source["label"]
        sources.append(
            {
                "label": label,
                "path": source["path"],
                "count": counts.get(label, 0),
            }
        )

    return {"status": "ok", "sources": sources}


@app.post("/api/sources")
async def add_source(source: SourceModel):
    config = load_config()

    label = sanitize_label(source.label)
    path = os.path.abspath(source.path.strip())

    if not label:
        raise HTTPException(status_code=400, detail="Label is required")

    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"Directory not found: {path}")

    for existing in config.get("sources", []):
        if existing["label"].lower() == label.lower():
            raise HTTPException(status_code=400, detail=f'Source "{label}" already exists')
        if os.path.abspath(existing["path"]) == path:
            raise HTTPException(status_code=400, detail="That folder is already added")

    config.setdefault("sources", []).append({"label": label, "path": path})
    save_config(config)

    return {"status": "ok", "source": {"label": label, "path": path}}


@app.delete("/api/sources/{label}")
async def remove_source(label: str):
    config = load_config()
    before = len(config.get("sources", []))
    config["sources"] = [s for s in config.get("sources", []) if s["label"] != label]

    if len(config["sources"]) == before:
        raise HTTPException(status_code=404, detail="Source not found")

    save_config(config)
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Recordings
# -----------------------------------------------------------------------------

@app.get("/api/recordings")
async def get_recordings(source: Optional[str] = None):
    config = load_config()
    recordings = scan_recordings(config.get("sources", []))

    if source:
        recordings = [r for r in recordings if r["source"] == source]

    return {"status": "ok", "recordings": recordings}


@app.post("/api/recordings/delete")
async def delete_recordings(req: DeleteRecordingsRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No files selected")

    config = load_config()
    allowed_roots = [s["path"] for s in config.get("sources", [])]

    deleted = 0
    failed = []

    for path in req.paths:
        real_path = os.path.realpath(path)

        if not os.path.isfile(real_path):
            failed.append({"path": path, "error": "File not found"})
            continue

        if not is_within_allowed_roots(real_path, allowed_roots):
            failed.append({"path": path, "error": "Outside configured sources"})
            continue

        try:
            os.remove(real_path)
            deleted += 1
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})

    return {"status": "ok", "deleted": deleted, "failed": failed}


@app.get("/api/recordings/info")
async def get_recording_info(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    duration = run_ffprobe_duration(path)
    extension = os.path.splitext(path)[1].lower()

    return {
        "status": "ok",
        "path": path,
        "duration": duration,
        "extension": extension,
        "needs_transcode_preview": extension not in DIRECT_PLAY_EXTENSIONS,
    }


@app.get("/api/recordings/thumbnail")
async def get_thumbnail(path: str, time: float = 1.0):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    thumb_path = build_thumbnail(path, time)
    return FileResponse(thumb_path, media_type="image/jpeg")


# -----------------------------------------------------------------------------
# Streaming
# -----------------------------------------------------------------------------

@app.get("/stream")
async def stream_video(path: str, transcode: bool = False):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = os.path.splitext(path)[1].lower()
    if transcode or ext not in DIRECT_PLAY_EXTENSIONS:
        preview_path = build_preview_mp4(path)
        return FileResponse(preview_path, media_type="video/mp4")

    return FileResponse(path, media_type=mime_type_for_path(path))


@app.get("/stream/clip")
async def stream_clip(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type=mime_type_for_path(path))


# -----------------------------------------------------------------------------
# Clips
# -----------------------------------------------------------------------------

@app.post("/api/clip")
async def create_clip(req: ClipRequest):
    if not os.path.isfile(req.filepath):
        raise HTTPException(status_code=404, detail="Source file not found")

    if req.end <= req.start:
        raise HTTPException(status_code=400, detail="End time must be greater than start time")

    clips_dir = get_clips_dir()

    safe_title = "".join(c for c in (req.title or "clip") if c.isalnum() or c in " -_").strip() or "clip"
    out_name = f"{safe_title}_{int(time.time())}.mp4"
    out_path = os.path.join(clips_dir, out_name)
    duration = req.end - req.start

    if req.lossless:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(req.start),
            "-i",
            req.filepath,
            "-t",
            str(duration),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(req.start),
            "-i",
            req.filepath,
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            out_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail="FFmpeg clip creation failed")

    meta = {
        "title": req.title,
        "game": req.game,
        "source": req.source_label,
        "start": req.start,
        "end": req.end,
        "lossless": req.lossless,
        "created": int(time.time()),
    }
    with open(out_path + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    stat = os.stat(out_path)
    return {
        "status": "ok",
        "clip": {
            "name": out_name,
            "path": out_path,
            "size": stat.st_size,
        },
    }


@app.get("/api/clips")
async def get_clips():
    clips_dir = get_clips_dir()
    clips = []

    for name in os.listdir(clips_dir):
        path = os.path.join(clips_dir, name)
        if not os.path.isfile(path):
            continue
        if not is_video_file(path):
            continue

        stat = os.stat(path)
        meta = None
        meta_path = path + ".json"
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = None

        clips.append(
            {
                "name": name,
                "path": path,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "meta": meta,
            }
        )

    clips.sort(key=lambda x: x["modified"], reverse=True)
    return {"status": "ok", "clips": clips}


@app.post("/api/clips/delete")
async def delete_clips(req: DeleteClipsRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No clips selected")

    clips_dir = os.path.realpath(get_clips_dir())
    deleted = 0
    failed = []

    for path in req.paths:
        real_path = os.path.realpath(path)
        if not os.path.isfile(real_path):
            failed.append({"path": path, "error": "Clip not found"})
            continue

        if not (real_path == clips_dir or real_path.startswith(clips_dir + os.sep)):
            failed.append({"path": path, "error": "Outside clips folder"})
            continue

        try:
            os.remove(real_path)
            meta_path = real_path + ".json"
            if os.path.exists(meta_path):
                os.remove(meta_path)
            deleted += 1
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})

    return {"status": "ok", "deleted": deleted, "failed": failed}


@app.get("/api/clips/download")
async def download_clip(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(path, filename=os.path.basename(path), media_type="application/octet-stream")


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    config = load_config()
    return {"status": "ok", **config}


@app.put("/api/settings")
async def update_settings(settings: SettingsModel):
    config = load_config()

    clips_path = os.path.abspath((settings.clips_path or "").strip() or DEFAULT_CLIPS_DIR)
    try:
        os.makedirs(clips_path, exist_ok=True)
    except OSError:
        raise HTTPException(status_code=400, detail=f"Cannot use clips folder: {clips_path}")

    config["auto_refresh"] = settings.auto_refresh
    config["refresh_interval"] = settings.refresh_interval
    config["clips_path"] = clips_path
    save_config(config)

    return {"status": "ok"}


# -----------------------------------------------------------------------------
# Folder Browser
# -----------------------------------------------------------------------------

@app.get("/api/browse")
async def browse_directory(path: str = "/"):
    target = os.path.abspath(path or "/")

    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")

    directories = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                directories.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {
        "status": "ok",
        "path": target,
        "parent": os.path.dirname(target) if target != "/" else "/",
        "directories": directories,
    }
