import hashlib
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="WebClipper")

DATA_DIR = os.getenv("WEBCLIPPER_DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
THUMBNAILS_DIR = os.path.join(DATA_DIR, "thumbnails")
PREVIEW_DIR = os.path.join(DATA_DIR, "preview")
DEFAULT_CLIPS_DIR = os.path.join(DATA_DIR, "clips")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts"}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov"}

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": True,
    "refresh_interval": 20,
    "clips_path": DEFAULT_CLIPS_DIR,
}


class SourceModel(BaseModel):
    label: str
    path: str


class SettingsModel(BaseModel):
    auto_refresh: bool
    refresh_interval: int = Field(ge=5, le=3600)
    clips_path: str


class DeletePathsRequest(BaseModel):
    paths: List[str] = Field(default_factory=list)


class ClipRequest(BaseModel):
    filepath: str
    source_label: str = ""
    start: float = 0.0
    end: float = 0.0
    title: str = "clip"
    game: str = "Unknown"
    mode: str = "copy"  # copy | transcode
    container: str = "mp4"  # mp4 | mkv
    selected_audio_tracks: List[int] = Field(default_factory=list)
    audio_mode: str = "keep"  # keep | mix
    audio_gains: Dict[str, float] = Field(default_factory=dict)


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    os.makedirs(DEFAULT_CLIPS_DIR, exist_ok=True)


ensure_dirs()


def load_config() -> dict:
    ensure_dirs()
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()

    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg or {})
    merged["sources"] = merged.get("sources", [])
    merged["clips_path"] = os.path.abspath(merged.get("clips_path") or DEFAULT_CLIPS_DIR)
    return merged


def save_config(config: dict) -> None:
    ensure_dirs()
    fd, temp_path = tempfile.mkstemp(prefix="config_", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.replace(temp_path, CONFIG_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def sanitize_label(label: str) -> str:
    return " ".join((label or "").strip().split())


def get_clips_dir() -> str:
    clips_dir = os.path.abspath(load_config().get("clips_path") or DEFAULT_CLIPS_DIR)
    os.makedirs(clips_dir, exist_ok=True)
    return clips_dir


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def mime_type_for_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def is_within_root(path: str, roots: List[str]) -> bool:
    real_path = os.path.realpath(path)
    for root in roots:
        real_root = os.path.realpath(root)
        if real_path == real_root or real_path.startswith(real_root + os.sep):
            return True
    return False


def ffprobe_json(path: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail="ffprobe failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid ffprobe output")


def probe_media(path: str) -> dict:
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        duration = 0.0

    audio_tracks = []
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue

        tags = stream.get("tags", {}) or {}
        track_number = len(audio_tracks) + 1
        audio_tracks.append(
            {
                "stream_index": int(stream.get("index")),
                "track_number": track_number,
                "codec": stream.get("codec_name", "unknown"),
                "channels": stream.get("channels", 0),
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
                "sample_rate": stream.get("sample_rate", ""),
            }
        )

    has_video = any(s.get("codec_type") == "video" for s in streams)
    return {
        "duration": duration,
        "audio_tracks": audio_tracks,
        "has_video": has_video,
        "streams": streams,
    }


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


def build_thumbnail(path: str, second: float = 1.0) -> str:
    real_path = os.path.realpath(path)
    key = hashlib.md5(real_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(THUMBNAILS_DIR, f"{key}.jpg")

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


def build_preview_mp4(path: str) -> str:
    real_path = os.path.realpath(path)
    key = hashlib.md5(real_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(PREVIEW_DIR, f"{key}.mp4")

    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(real_path):
        return out_path

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        real_path,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail="Preview generation failed")

    return out_path


def safe_title(value: str) -> str:
    cleaned = "".join(c for c in (value or "clip") if c.isalnum() or c in " -_").strip()
    return cleaned or "clip"


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail})


@app.exception_handler(Exception)
async def generic_exception_handler(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"status": "error", "detail": str(exc)})


@app.get("/", response_class=HTMLResponse)
async def index():
    template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/sources")
async def get_sources():
    config = load_config()
    recordings = scan_recordings(config.get("sources", []))
    counts = {}
    for item in recordings:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    sources = []
    for source in config.get("sources", []):
        sources.append(
            {
                "label": source["label"],
                "path": source["path"],
                "count": counts.get(source["label"], 0),
            }
        )

    return {"status": "ok", "sources": sources}


@app.post("/api/sources")
async def add_source(source: SourceModel):
    config = load_config()
    label = sanitize_label(source.label)
    path = os.path.abspath((source.path or "").strip())

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


@app.get("/api/browse")
async def browse_directory(path: str = "/"):
    target = os.path.abspath(path or "/")
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Directory not found")

    try:
        directories = [
            {"name": entry.name, "path": entry.path}
            for entry in sorted(os.scandir(target), key=lambda e: e.name.lower())
            if entry.is_dir() and not entry.name.startswith(".")
        ]
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {
        "status": "ok",
        "path": target,
        "parent": os.path.dirname(target) if target != "/" else "/",
        "directories": directories,
    }


@app.get("/api/recordings")
async def get_recordings(source: Optional[str] = None):
    config = load_config()
    recordings = scan_recordings(config.get("sources", []))
    if source:
        recordings = [r for r in recordings if r["source"] == source]
    return {"status": "ok", "recordings": recordings}


@app.post("/api/recordings/delete")
async def delete_recordings(req: DeletePathsRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No files selected")

    config = load_config()
    roots = [s["path"] for s in config.get("sources", [])]
    deleted = 0
    failed = []

    for path in req.paths:
        real = os.path.realpath(path)

        if not os.path.isfile(real):
            failed.append({"path": path, "error": "File not found"})
            continue

        if not is_within_root(real, roots):
            failed.append({"path": path, "error": "Outside configured sources"})
            continue

        try:
            os.remove(real)
            deleted += 1
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})

    return {"status": "ok", "deleted": deleted, "failed": failed}


@app.get("/api/recordings/info")
async def get_recording_info(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    probe = probe_media(path)
    extension = os.path.splitext(path)[1].lower()

    return {
        "status": "ok",
        "path": path,
        "duration": probe["duration"],
        "extension": extension,
        "needs_transcode_preview": extension not in DIRECT_PLAY_EXTENSIONS,
        "audio_tracks": probe["audio_tracks"],
        "has_video": probe["has_video"],
    }


@app.get("/api/recordings/thumbnail")
async def get_thumbnail(path: str, time_pos: float = 1.0):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    thumb = build_thumbnail(path, time_pos)
    return FileResponse(thumb, media_type="image/jpeg")


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


@app.post("/api/clip")
async def create_clip(req: ClipRequest):
    if not os.path.isfile(req.filepath):
        raise HTTPException(status_code=404, detail="Source file not found")

    probe = probe_media(req.filepath)
    duration = probe["duration"]
    start = max(0.0, float(req.start or 0.0))
    end = float(req.end or duration)

    if end <= start:
        raise HTTPException(status_code=400, detail="End time must be greater than start time")
    if duration > 0 and end > duration:
        end = duration

    selected = req.selected_audio_tracks or [t["stream_index"] for t in probe["audio_tracks"]]
    selected = [int(x) for x in selected if int(x) in [t["stream_index"] for t in probe["audio_tracks"]]]

    container = (req.container or "mp4").lower().lstrip(".")
    if container not in {"mp4", "mkv"}:
        raise HTTPException(status_code=400, detail="Container must be mp4 or mkv")

    mode = (req.mode or "copy").lower()
    if mode not in {"copy", "transcode"}:
        raise HTTPException(status_code=400, detail="Mode must be copy or transcode")

    audio_mode = (req.audio_mode or "keep").lower()
    if audio_mode not in {"keep", "mix"}:
        raise HTTPException(status_code=400, detail="Audio mode must be keep or mix")

    if audio_mode == "mix" and mode != "transcode" and len(selected) > 1:
        raise HTTPException(status_code=400, detail="Audio mixing requires Accurate Clip mode")

    clips_dir = get_clips_dir()
    base_name = safe_title(req.title)
    output_path = os.path.join(clips_dir, f"{base_name}_{int(time.time())}.{container}")

    clip_duration = max(0.01, end - start)

    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", req.filepath, "-t", str(clip_duration)]

    if mode == "copy":
        cmd += ["-map", "0:v:0?"]

        if selected:
            for stream_idx in selected:
                cmd += ["-map", f"0:{stream_idx}"]
        else:
            cmd += ["-an"]

        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]

        if container == "mp4":
            cmd += ["-movflags", "+faststart"]

    else:
        filter_parts = []
        audio_map_targets = []

        cmd += ["-map", "0:v:0?"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]

        if container == "mp4":
            cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

        if not selected:
            cmd += ["-an"]
        else:
            if audio_mode == "mix" and len(selected) > 1:
                mix_inputs = []
                for idx, stream_idx in enumerate(selected):
                    gain = float(req.audio_gains.get(str(stream_idx), 0.0))
                    filter_parts.append(f"[0:{stream_idx}]volume={gain}dB[a{idx}]")
                    mix_inputs.append(f"[a{idx}]")
                filter_parts.append("".join(mix_inputs) + f"amix=inputs={len(selected)}:normalize=0[mixed]")
                audio_map_targets = ["[mixed]"]
                cmd += ["-c:a", "aac", "-b:a", "320k", "-ac", "2"]
            else:
                mapped_filtered = []
                mapped_direct = []
                for idx, stream_idx in enumerate(selected):
                    gain = float(req.audio_gains.get(str(stream_idx), 0.0))
                    if abs(gain) > 0.01:
                        label = f"[aud{idx}]"
                        filter_parts.append(f"[0:{stream_idx}]volume={gain}dB{label}")
                        mapped_filtered.append(label)
                    else:
                        mapped_direct.append(f"0:{stream_idx}")

                audio_map_targets = mapped_filtered + mapped_direct
                cmd += ["-c:a", "aac", "-b:a", "256k"]

            if filter_parts:
                cmd += ["-filter_complex", ";".join(filter_parts)]

            for target in audio_map_targets:
                cmd += ["-map", target]

    cmd.append(output_path)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not os.path.exists(output_path):
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg failed: {(result.stderr or '').strip()[:800]}",
        )

    metadata = {
        "title": req.title,
        "game": req.game,
        "source": req.source_label,
        "start": start,
        "end": end,
        "mode": mode,
        "container": container,
        "audio_mode": audio_mode,
        "selected_audio_tracks": selected,
        "audio_gains": req.audio_gains,
        "created": int(time.time()),
    }
    with open(output_path + ".json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    stat = os.stat(output_path)
    return {
        "status": "ok",
        "clip": {
            "name": os.path.basename(output_path),
            "path": output_path,
            "size": stat.st_size,
            "meta": metadata,
        },
    }


@app.get("/api/clips")
async def get_clips():
    clips_dir = get_clips_dir()
    clips = []

    for name in os.listdir(clips_dir):
        path = os.path.join(clips_dir, name)
        if not os.path.isfile(path) or not is_video_file(path):
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

    clips.sort(key=lambda item: item["modified"], reverse=True)
    return {"status": "ok", "clips": clips}


@app.post("/api/clips/delete")
async def delete_clips(req: DeletePathsRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No clips selected")

    clips_dir = os.path.realpath(get_clips_dir())
    deleted = 0
    failed = []

    for path in req.paths:
        real = os.path.realpath(path)

        if not os.path.isfile(real):
            failed.append({"path": path, "error": "Clip not found"})
            continue

        if not (real == clips_dir or real.startswith(clips_dir + os.sep)):
            failed.append({"path": path, "error": "Outside clips folder"})
            continue

        try:
            os.remove(real)
            meta_path = real + ".json"
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
