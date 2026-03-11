import os
import json
import subprocess
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="WebClipper")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CLIPS_DIR = DATA_DIR / "clips"
CONFIG_FILE = DATA_DIR / "config.json"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)


# ── Config Persistence ──────────────────────────────────────────────

def load_config() -> dict:
    default = {
        "sources": [],
        "clips_dir": str(CLIPS_DIR),
        "auto_refresh": True,
        "refresh_interval": 20,
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            default.update(saved)
        except Exception:
            pass
    return default


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── Models ──────────────────────────────────────────────────────────

class SourceModel(BaseModel):
    label: str
    path: str


class ClipRequest(BaseModel):
    filepath: str  # full path relative to source root
    source_label: str
    start: float
    end: float
    title: Optional[str] = None
    game: Optional[str] = None
    lossless: bool = True
    audio_tracks: Optional[list[dict]] = None  # [{index, enabled, volume}]


class SettingsUpdate(BaseModel):
    auto_refresh: Optional[bool] = None
    refresh_interval: Optional[int] = None


# ── Helpers ─────────────────────────────────────────────────────────

def probe_file(filepath: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(filepath)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout)


def get_video_info(filepath: Path) -> dict:
    info = probe_file(filepath)
    fmt = info.get("format", {})
    streams = info.get("streams", [])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    fps = None
    if video_stream.get("r_frame_rate") and "/" in str(video_stream.get("r_frame_rate", "")):
        try:
            num, den = video_stream["r_frame_rate"].split("/")
            fps = round(int(num) / int(den), 2) if int(den) != 0 else None
        except (ValueError, ZeroDivisionError):
            pass

    audio_info = []
    for i, a in enumerate(audio_streams):
        audio_info.append({
            "index": a.get("index"),
            "codec": a.get("codec_name", ""),
            "channels": a.get("channels", 0),
            "sample_rate": a.get("sample_rate", ""),
            "language": a.get("tags", {}).get("language", "und"),
            "title": a.get("tags", {}).get("title", f"Track {i+1}"),
        })

    return {
        "duration": float(fmt.get("duration", 0)),
        "size": int(fmt.get("size", 0)),
        "bit_rate": int(fmt.get("bit_rate", 0)) if fmt.get("bit_rate") else None,
        "format_name": fmt.get("format_name", ""),
        "video_codec": video_stream.get("codec_name", ""),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "fps": fps,
        "audio_tracks": audio_info,
    }


def generate_thumbnail(filepath: Path, time: float = 1.0) -> Optional[Path]:
    thumb_dir = DATA_DIR / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    # Use a stable hash so we cache thumbnails
    name_hash = str(abs(hash(str(filepath))))
    thumb_path = thumb_dir / f"{name_hash}_{time:.0f}.jpg"
    if thumb_path.exists():
        return thumb_path
    cmd = [
        "ffmpeg", "-y", "-ss", str(time),
        "-i", str(filepath),
        "-vframes", "1", "-q:v", "5",
        "-vf", "scale=400:-1",
        str(thumb_path)
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=15)
    if thumb_path.exists():
        return thumb_path
    return None


# ── Routes: Pages ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html") as f:
        return f.read()


# ── Routes: Sources ─────────────────────────────────────────────────

@app.get("/api/sources")
async def list_sources():
    config = load_config()
    sources = []
    for src in config["sources"]:
        p = Path(src["path"])
        count = 0
        if p.exists():
            count = sum(1 for f in p.iterdir()
                       if f.is_file() and f.suffix.lower() in
                       (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mts"))
        sources.append({**src, "count": count})
    return {"sources": sources}


@app.post("/api/sources")
async def add_source(source: SourceModel):
    config = load_config()
    # Check if path exists
    p = Path(source.path)
    if not p.exists():
        raise HTTPException(400, f"Path does not exist: {source.path}")
    if not p.is_dir():
        raise HTTPException(400, f"Path is not a directory: {source.path}")
    # Check for duplicate labels
    if any(s["label"] == source.label for s in config["sources"]):
        raise HTTPException(400, f"Source with label '{source.label}' already exists")
    config["sources"].append({"label": source.label, "path": source.path})
    save_config(config)
    return {"status": "added"}


@app.delete("/api/sources/{label}")
async def remove_source(label: str):
    config = load_config()
    config["sources"] = [s for s in config["sources"] if s["label"] != label]
    save_config(config)
    return {"status": "removed"}


# ── Routes: Recordings ──────────────────────────────────────────────

@app.get("/api/recordings")
async def list_recordings(source: Optional[str] = None):
    """List recordings, optionally filtered by source label."""
    config = load_config()
    recordings = []
    video_exts = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mts")

    sources_to_scan = config["sources"]
    if source:
        sources_to_scan = [s for s in config["sources"] if s["label"] == source]

    for src in sources_to_scan:
        p = Path(src["path"])
        if not p.exists():
            continue
        for f in sorted(p.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in video_exts:
                stat = f.stat()
                recordings.append({
                    "name": f.name,
                    "path": str(f),
                    "source": src["label"],
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
    return {"recordings": recordings}


@app.get("/api/recordings/info")
async def recording_info(path: str):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    info = get_video_info(filepath)
    info["name"] = filepath.name
    info["path"] = str(filepath)
    return info


@app.get("/api/recordings/thumbnail")
async def recording_thumbnail(path: str, time: float = 1.0):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    thumb = generate_thumbnail(filepath, time)
    if thumb:
        return FileResponse(thumb, media_type="image/jpeg")
    raise HTTPException(500, "Failed to generate thumbnail")


@app.get("/stream")
async def stream_video(path: str):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(filepath)


# ── Routes: Clipping ───────────────────────────────────────────────

@app.post("/api/clip")
async def create_clip(req: ClipRequest):
    source = Path(req.filepath)
    if not source.exists():
        raise HTTPException(404, "Source video not found")

    if req.end <= req.start:
        raise HTTPException(400, "End time must be after start time")

    config = load_config()
    clips_dir = Path(config.get("clips_dir", str(CLIPS_DIR)))
    clips_dir.mkdir(parents=True, exist_ok=True)

    duration = req.end - req.start
    ext = source.suffix

    title = req.title or source.stem
    out_name = f"{title}{ext}"
    # Avoid overwriting
    counter = 1
    while (clips_dir / out_name).exists():
        out_name = f"{title}_{counter}{ext}"
        counter += 1

    output = clips_dir / out_name

    if req.lossless:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(req.start),
            "-i", str(source),
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-map", "0",
            str(output)
        ]
    else:
        # Build audio filter for track mixing
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(req.start),
            "-i", str(source),
            "-t", str(duration),
        ]

        if req.audio_tracks:
            # Handle audio track selection and volume
            audio_maps = []
            filter_parts = []
            enabled_tracks = [t for t in req.audio_tracks if t.get("enabled", True)]

            if len(enabled_tracks) == 0:
                cmd.extend(["-an"])
            elif len(enabled_tracks) == 1:
                t = enabled_tracks[0]
                vol = t.get("volume", 100) / 100.0
                cmd.extend(["-map", "0:v:0", "-map", f"0:a:{t['index']}"])
                if vol != 1.0:
                    cmd.extend(["-af", f"volume={vol}"])
            else:
                # Mix multiple audio tracks
                filter_str = ""
                for i, t in enumerate(enabled_tracks):
                    vol = t.get("volume", 100) / 100.0
                    filter_str += f"[0:a:{t['index']}]volume={vol}[a{i}];"
                inputs = "".join(f"[a{i}]" for i in range(len(enabled_tracks)))
                filter_str += f"{inputs}amix=inputs={len(enabled_tracks)}:duration=first[aout]"
                cmd.extend([
                    "-map", "0:v:0",
                    "-filter_complex", filter_str,
                    "-map", "[aout]",
                ])

            cmd.extend([
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
            ])
        else:
            cmd.extend([
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
            ])

        cmd.extend(["-avoid_negative_ts", "make_zero", str(output)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise HTTPException(500, f"FFmpeg error: {result.stderr[-500:]}")

    # Save clip metadata
    meta = {
        "title": title,
        "source": req.source_label,
        "source_file": req.filepath,
        "game": req.game or "Unknown",
        "start": req.start,
        "end": req.end,
        "lossless": req.lossless,
    }
    meta_file = clips_dir / f"{out_name}.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2)

    return {
        "name": out_name,
        "size": output.stat().st_size,
        "duration": duration,
        "lossless": req.lossless,
    }


# ── Routes: Clips Management ──────────────────────────────────────

@app.get("/api/clips")
async def list_clips():
    config = load_config()
    clips_dir = Path(config.get("clips_dir", str(CLIPS_DIR)))
    clips = []
    video_exts = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v")

    if clips_dir.exists():
        for f in sorted(clips_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in video_exts:
                stat = f.stat()
                clip = {
                    "name": f.name,
                    "path": str(f),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
                # Load metadata if exists
                meta_file = clips_dir / f"{f.name}.json"
                if meta_file.exists():
                    try:
                        with open(meta_file) as mf:
                            clip["meta"] = json.load(mf)
                    except Exception:
                        pass
                clips.append(clip)
    return {"clips": clips}


@app.get("/api/clips/download")
async def download_clip(path: str):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(filepath, filename=filepath.name)


@app.get("/stream/clip")
async def stream_clip(path: str):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(filepath)


@app.delete("/api/clips")
async def delete_clip(path: str):
    filepath = Path(path)
    if not filepath.exists():
        raise HTTPException(404, "Clip not found")
    filepath.unlink()
    # Remove metadata too
    meta_file = Path(f"{filepath}.json")
    if meta_file.exists():
        meta_file.unlink()
    return {"status": "deleted"}


# ── Routes: Settings ───────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return load_config()


@app.put("/api/settings")
async def update_settings(settings: SettingsUpdate):
    config = load_config()
    if settings.auto_refresh is not None:
        config["auto_refresh"] = settings.auto_refresh
    if settings.refresh_interval is not None:
        config["refresh_interval"] = settings.refresh_interval
    save_config(config)
    return config


# ── Routes: Browse filesystem (for settings) ──────────────────────

@app.get("/api/browse")
async def browse_directory(path: str = "/"):
    """Browse directories on the server for setting up sources."""
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, "Invalid directory")
    dirs = []
    for item in sorted(p.iterdir()):
        if item.is_dir() and not item.name.startswith('.'):
            dirs.append({
                "name": item.name,
                "path": str(item),
            })
    return {"current": str(p), "directories": dirs}
