"""FastAPI app: config, sources, recordings, preview, clips, remux jobs, folder browser."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import clips_store, config, recordings, remux_jobs, sources
from app.ffmpeg_utils import (
    ensure_preview,
    generate_thumbnail,
    get_file_info,
    create_clip as ffmpeg_create_clip,
)
from app.recordings import thumbnail_path_for_file

# --- Pydantic models ---


class AddSourceBody(BaseModel):
    label: str
    path: str


class DeleteSourceBody(BaseModel):
    path: str


class SettingsBody(BaseModel):
    auto_refresh: bool | None = None
    refresh_interval_seconds: int | None = None
    clips_output_folder: str | None = None


class BrowseBody(BaseModel):
    path: str


class SelectFolderBody(BaseModel):
    path: str


class DeleteRecordingsBody(BaseModel):
    paths: list[str]


class RemuxBody(BaseModel):
    paths: list[str]


class CreateClipBody(BaseModel):
    source_path: str
    clip_title: str = ""
    game_name: str = ""
    start_sec: float
    end_sec: float
    export_mode: str = "fast_cut"
    output_container: str = "mp4"
    audio_mode: str = "separate"
    selected_audio_indices: list[int] = None
    gains: dict[str, float] = None  # "0" -> 1.0

    def __init__(self, **data):
        super().__init__(**data)
        if self.selected_audio_indices is None:
            self.selected_audio_indices = []
        if self.gains is None:
            self.gains = {}


class DeleteClipsBody(BaseModel):
    paths: list[str]


# --- Router ---

router = APIRouter()


@router.get("/api/sources")
def api_sources():
    return {"sources": sources.get_sources()}


@router.post("/api/sources")
def api_add_source(body: AddSourceBody):
    sources.add_source(body.label, body.path)
    return {"sources": sources.get_sources()}


@router.delete("/api/sources")
def api_delete_source(body: DeleteSourceBody):
    sources.delete_source(body.path)
    return {"sources": sources.get_sources()}


@router.get("/api/settings")
def api_get_settings():
    c = config.load_config()
    return {
        "auto_refresh": c.get("auto_refresh", False),
        "refresh_interval_seconds": c.get("refresh_interval_seconds", 60),
        "clips_output_folder": c.get("clips_output_folder", str(config.CLIPS_DIR)),
    }


@router.put("/api/settings")
def api_update_settings(body: SettingsBody):
    c = config.load_config()
    if body.auto_refresh is not None:
        c["auto_refresh"] = body.auto_refresh
    if body.refresh_interval_seconds is not None:
        c["refresh_interval_seconds"] = body.refresh_interval_seconds
    if body.clips_output_folder is not None:
        c["clips_output_folder"] = body.clips_output_folder
    config.save_config(c)
    return {"ok": True}


@router.post("/api/browse")
def api_browse(body: BrowseBody):
    """List directories under path. For folder picker."""
    p = Path(body.path)
    if not p.exists() or not p.is_dir():
        return {"path": body.path, "entries": [], "parent": None}
    parent = str(p.parent) if p.parent != p else None
    entries = []
    try:
        for e in sorted(p.iterdir()):
            if e.is_dir():
                entries.append({"name": e.name, "path": str(e.resolve()), "is_dir": True})
    except PermissionError:
        pass
    return {"path": str(p.resolve()), "entries": entries, "parent": parent}


@router.get("/api/recordings")
def api_recordings(source: str | None = None):
    return {"recordings": recordings.scan_recordings(source)}


@router.get("/api/recordings/info")
def api_recording_info(path: str):
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Recording not found")
    probe = get_file_info(path)
    return {"recording": info, "probe": probe}


@router.get("/api/recordings/thumbnail")
def api_thumbnail(path: str):
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Not found")
    thumb_path = thumbnail_path_for_file(path)
    if not thumb_path.exists():
        generate_thumbnail(path, thumb_path)
    if not thumb_path.exists():
        raise HTTPException(404, "Thumbnail not generated")
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get("/api/recordings/preview")
def api_preview_info(path: str):
    """Return preview strategy and path to use for playback."""
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Not found")
    preview_path, strategy = ensure_preview(path)
    if not preview_path:
        return {"strategy": strategy, "url": None, "error": "Preview unavailable"}
    # Frontend will use /api/stream/preview?path=... for cached previews
    return {"strategy": strategy, "url": f"/api/stream/preview?path={path}", "path": preview_path}


@router.delete("/api/recordings")
def api_delete_recordings(body: DeleteRecordingsBody):
    deleted = recordings.delete_recordings(body.paths)
    return {"deleted": deleted}


@router.post("/api/remux")
def api_remux(body: RemuxBody):
    mkv_paths = [p for p in body.paths if Path(p).suffix.lower() == ".mkv"]
    if not mkv_paths:
        return {"job_id": None, "message": "No MKV files selected"}
    job_id = remux_jobs.create_job(mkv_paths)
    return {"job_id": job_id, "total": len(mkv_paths)}


@router.get("/api/jobs")
def api_jobs():
    return {"jobs": remux_jobs.get_all_jobs()}


@router.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = remux_jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/api/clips/create")
def api_create_clip(body: CreateClipBody):
    rec = recordings.get_recording_by_path(body.source_path)
    if not rec:
        raise HTTPException(404, "Recording not found")
    base = clips_store.get_clips_dir()
    base.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in (body.clip_title or rec["name"]) if c.isalnum() or c in " -_.").strip() or "clip"
    out_path = str(base / safe_name)
    gains_int = {}
    for k, v in (body.gains or {}).items():
        try:
            gains_int[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    ok, result = ffmpeg_create_clip(
        body.source_path,
        out_path,
        body.start_sec,
        body.end_sec,
        body.export_mode,
        body.output_container,
        body.audio_mode,
        body.selected_audio_indices or [],
        gains_int,
        {"title": body.clip_title, "game": body.game_name, "source": body.source_path},
    )
    if not ok:
        raise HTTPException(500, result)
    return {"path": result, "ok": True}


@router.get("/api/clips")
def api_clips():
    return {"clips": clips_store.list_clips()}


@router.delete("/api/clips")
def api_delete_clips(body: DeleteClipsBody):
    deleted = clips_store.delete_clips(body.paths)
    return {"deleted": deleted}


# --- Streaming: direct file or preview cache ---

def _stream_file(path: str, media_type: str = "video/mp4"):
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path_obj, media_type=media_type)


@router.get("/api/stream/preview")
def api_stream_preview(path: str):
    """Stream preview file (may be cached remux/transcode)."""
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Not found")
    preview_path, _ = ensure_preview(path)
    if not preview_path:
        raise HTTPException(404, "Preview not ready")
    return FileResponse(preview_path, media_type="video/mp4")


@router.get("/api/stream/recording")
def api_stream_recording(path: str):
    """Stream recording file directly. MKV is not supported; use preview or remux first."""
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Not found")
    if Path(path).suffix.lower() == ".mkv":
        raise HTTPException(
            400,
            "MKV files cannot be played directly. Use Remux to convert to MP4, or wait for the preview cache to be ready.",
        )
    return FileResponse(path, media_type="video/mp4")


@router.get("/api/stream/clip")
def api_stream_clip(path: str):
    """Stream a clip file."""
    clips = clips_store.list_clips()
    if not any(c["path"] == path for c in clips):
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4")


# --- App mount ---

def create_app():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    config.ensure_dirs()

    app = FastAPI(title="WebClipper", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)

    # Serve SPA: index.html for any non-API route
    frontend = Path(__file__).resolve().parent.parent / "frontend"
    index_file = frontend / "index.html"

    @app.get("/")
    def index():
        if index_file.exists():
            return FileResponse(index_file, media_type="text/html")
        return {"app": "WebClipper", "docs": "/docs"}

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api"):
            raise HTTPException(404, "Not found")
        if index_file.exists():
            return FileResponse(index_file, media_type="text/html")
        raise HTTPException(404, "Not found")

    return app


app = create_app()
=======
class SettingsModel(BaseModel):
    auto_refresh: bool
    refresh_interval: int = Field(ge=5, le=3600)
    clips_path: str


class DeletePathsRequest(BaseModel):
    paths: List[str] = Field(default_factory=list)


class RemuxRequest(BaseModel):
    paths: List[str] = Field(default_factory=list)


class ClipRequest(BaseModel):
    filepath: str
    source_label: str = ""
    start: float = 0.0
    end: float = 0.0
    title: str = "clip"
    game: str = "Unknown"
    mode: str = "copy"
    container: str = "mp4"
    selected_audio_tracks: List[int] = Field(default_factory=list)
    audio_mode: str = "keep"
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
        timeout=30,
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
    video_codec = ""
    audio_codec = ""
    has_video = False

    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not video_codec:
            video_codec = (stream.get("codec_name") or "").lower()
            has_video = True
        if codec_type == "audio" and not audio_codec:
            audio_codec = (stream.get("codec_name") or "").lower()

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

    return {
        "duration": duration,
        "audio_tracks": audio_tracks,
        "has_video": has_video,
        "streams": streams,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
    }


def choose_preview_strategy(path: str, probe: dict) -> str:
    ext = os.path.splitext(path)[1].lower()
    video_codec = probe.get("video_codec", "")
    audio_codec = probe.get("audio_codec", "")

    if ext in DIRECT_PLAY_EXTENSIONS and video_codec in BROWSER_FRIENDLY_VIDEO and (
        not audio_codec or audio_codec in BROWSER_FRIENDLY_AUDIO
    ):
        return "direct"

    if video_codec == "h264":
        if not audio_codec or audio_codec in {"aac", "mp3"}:
            return "remux"
        return "audio_transcode"

    return "proxy_transcode"


def build_preview_asset(path: str, probe: dict) -> dict:
    strategy = choose_preview_strategy(path, probe)
    if strategy == "direct":
        encoded = quote(path, safe="")
        return {"strategy": strategy, "url": f"/stream/direct?path={encoded}"}

    real_path = os.path.realpath(path)
    mtime = str(os.path.getmtime(real_path))
    key = hashlib.md5(f"{real_path}|{mtime}|{strategy}".encode("utf-8")).hexdigest()
    out_name = f"{key}.mp4"
    out_path = os.path.join(PREVIEW_DIR, out_name)

    if os.path.exists(out_path):
        return {"strategy": strategy, "url": f"/preview-cache/{out_name}"}

    if strategy == "remux":
        cmd = [
            "ffmpeg", "-y", "-i", real_path,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-dn", "-sn", "-c", "copy", "-movflags", "+faststart", out_path,
        ]
    elif strategy == "audio_transcode":
        cmd = [
            "ffmpeg", "-y", "-i", real_path,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-dn", "-sn", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", real_path,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-dn", "-sn", "-vf", "scale='min(1920,iw)':-2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path,
        ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail=f"Preview build failed: {(result.stderr or '').strip()[:700]}")

    return {"strategy": strategy, "url": f"/preview-cache/{out_name}"}


def scan_recordings(sources: List[dict]) -> List[dict]:
    recordings: List[dict] = []

    for source in sources:
        label = source.get("label", "").strip()
        root = source.get("path", "").strip()
        if not label or not root or not os.path.isdir(root):
            continue
        try:
            for entry in os.scandir(root):
                if not entry.is_file() or not is_video_file(entry.path):
                    continue
                stat = entry.stat()
                recordings.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "source": label,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "ext": Path(entry.path).suffix.lower(),
                    }
                )
        except PermissionError:
            continue

    recordings.sort(key=lambda x: x["modified"], reverse=True)
    return recordings


def build_thumbnail(path: str, second: float = 1.0) -> str:
    real_path = os.path.realpath(path)
    mtime = str(os.path.getmtime(real_path))
    key = hashlib.md5(f"{real_path}|{mtime}".encode("utf-8")).hexdigest()
    out_path = os.path.join(THUMBNAILS_DIR, f"{key}.jpg")

    if os.path.exists(out_path):
        return out_path

    cmd = [
        "ffmpeg", "-y", "-ss", str(second), "-i", real_path,
        "-frames:v", "1", "-q:v", "5", "-vf", "scale=480:-2", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise HTTPException(status_code=500, detail="Thumbnail generation failed")
    return out_path


def safe_title(value: str) -> str:
    cleaned = "".join(c for c in (value or "clip") if c.isalnum() or c in " -_").strip()
    return cleaned or "clip"


def set_job(job_id: str, patch: dict) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(patch)


def get_job(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return json.loads(json.dumps(job))


def start_remux_job(paths: List[str], roots: List[str]) -> str:
    job_id = uuid.uuid4().hex
    items = []
    for path in paths:
        items.append({
            "source_path": path,
            "output_path": "",
            "status": "queued",
            "progress": 0.0,
            "error": "",
        })

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "type": "remux",
            "status": "queued",
            "progress": 0.0,
            "created": int(time.time()),
            "items": items,
            "message": "Queued",
        }

    thread = threading.Thread(target=run_remux_job, args=(job_id, paths, roots), daemon=True)
    thread.start()
    return job_id


def parse_progress_line(line: str) -> tuple[str, str]:
    if "=" not in line:
        return "", ""
    key, value = line.strip().split("=", 1)
    return key.strip(), value.strip()


def remux_one_file(job_id: str, idx: int, source_path: str, total_count: int) -> None:
    item_prefix = idx / max(total_count, 1)
    item_share = 1 / max(total_count, 1)

    real_source = os.path.realpath(source_path)
    ext = Path(real_source).suffix.lower()

    if not os.path.isfile(real_source):
        set_job(job_id, {"message": f"Missing file: {source_path}"})
        with JOBS_LOCK:
            JOBS[job_id]["items"][idx]["status"] = "failed"
            JOBS[job_id]["items"][idx]["error"] = "File not found"
        return

    if ext != ".mkv":
        with JOBS_LOCK:
            JOBS[job_id]["items"][idx]["status"] = "skipped"
            JOBS[job_id]["items"][idx]["progress"] = 100.0
        set_job(job_id, {"progress": round((item_prefix + item_share) * 100, 1)})
        return

    probe = probe_media(real_source)
    duration = max(float(probe.get("duration") or 0), 0.01)
    output_path = os.path.splitext(real_source)[0] + ".mp4"
    temp_output = output_path + f".__remux_{uuid.uuid4().hex}.tmp"

    with JOBS_LOCK:
        JOBS[job_id]["items"][idx]["status"] = "running"
        JOBS[job_id]["items"][idx]["output_path"] = output_path

    cmd = [
        "ffmpeg",
        "-y",
        "-i", real_source,
        "-map", "0",
        "-dn", "-sn",
        "-c", "copy",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        temp_output,
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    last_progress = 0.0
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            key, value = parse_progress_line(raw_line)
            if key == "out_time_ms":
                try:
                    out_time = float(value) / 1_000_000.0
                    current = max(0.0, min(out_time / duration, 1.0))
                    last_progress = current
                    overall = (item_prefix + (current * item_share)) * 100
                    with JOBS_LOCK:
                        JOBS[job_id]["items"][idx]["progress"] = round(current * 100, 1)
                        JOBS[job_id]["progress"] = round(overall, 1)
                        JOBS[job_id]["message"] = f"Remuxing {os.path.basename(real_source)}"
                except Exception:
                    pass
            elif key == "progress" and value == "end":
                last_progress = 1.0
    finally:
        proc.wait()

    if proc.returncode != 0 or not os.path.exists(temp_output):
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass
        with JOBS_LOCK:
            JOBS[job_id]["items"][idx]["status"] = "failed"
            JOBS[job_id]["items"][idx]["error"] = "ffmpeg remux failed"
            JOBS[job_id]["items"][idx]["progress"] = round(last_progress * 100, 1)
        return

    try:
        if os.path.exists(output_path):
            os.remove(output_path)
        os.replace(temp_output, output_path)
        os.remove(real_source)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["items"][idx]["status"] = "failed"
            JOBS[job_id]["items"][idx]["error"] = str(exc)
        return

    with JOBS_LOCK:
        JOBS[job_id]["items"][idx]["status"] = "done"
        JOBS[job_id]["items"][idx]["progress"] = 100.0
        JOBS[job_id]["progress"] = round((item_prefix + item_share) * 100, 1)


def run_remux_job(job_id: str, paths: List[str], roots: List[str]) -> None:
    set_job(job_id, {"status": "running", "message": "Starting remux...", "progress": 0.0})

    filtered = []
    for path in paths:
        real = os.path.realpath(path)
        if os.path.isfile(real) and is_within_root(real, roots):
            filtered.append(real)

    total = len(filtered)
    if total == 0:
        set_job(job_id, {"status": "failed", "message": "No valid files to remux", "progress": 0.0})
        return

    for idx, path in enumerate(filtered):
        remux_one_file(job_id, idx, path, total)

    with JOBS_LOCK:
        items = JOBS[job_id]["items"]
        if any(item["status"] == "failed" for item in items):
            JOBS[job_id]["status"] = "partial"
            JOBS[job_id]["message"] = "Remux finished with some failures"
        else:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["message"] = "Remux finished"
        JOBS[job_id]["progress"] = 100.0


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
        sources.append({
            "label": source["label"],
            "path": source["path"],
            "count": counts.get(source["label"], 0),
        })

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

    parent = target if target == "/" else os.path.dirname(target) or "/"
    return {"status": "ok", "path": target, "parent": parent, "directories": directories}


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


@app.post("/api/recordings/remux")
async def remux_recordings(req: RemuxRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No files selected")

    config = load_config()
    roots = [s["path"] for s in config.get("sources", [])]
    job_id = start_remux_job(req.paths, roots)
    return {"status": "ok", "job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    return {"status": "ok", "job": get_job(job_id)}


@app.get("/api/jobs")
async def list_jobs():
    with JOBS_LOCK:
        jobs = list(JOBS.values())[-10:]
    return {"status": "ok", "jobs": jobs}


@app.get("/api/recordings/info")
async def get_recording_info(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    probe = probe_media(path)
    preview_strategy = choose_preview_strategy(path, probe)
    extension = os.path.splitext(path)[1].lower()

    return {
        "status": "ok",
        "path": path,
        "duration": probe["duration"],
        "extension": extension,
        "preview_strategy": preview_strategy,
        "audio_tracks": probe["audio_tracks"],
        "has_video": probe["has_video"],
    }


@app.get("/api/recordings/preview")
async def get_recording_preview(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    probe = probe_media(path)
    preview = build_preview_asset(path, probe)
    return {"status": "ok", **preview}


@app.get("/api/recordings/thumbnail")
async def get_thumbnail(path: str, time_pos: float = 1.0):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    thumb = build_thumbnail(path, time_pos)
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/preview-cache/{name}")
async def preview_cache(name: str):
    safe_name = os.path.basename(name)
    preview_path = os.path.join(PREVIEW_DIR, safe_name)
    if not os.path.isfile(preview_path):
        raise HTTPException(status_code=404, detail="Preview not found")
    return FileResponse(preview_path, media_type="video/mp4")


@app.get("/stream/direct")
async def stream_direct(path: str):
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
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

    valid_streams = [t["stream_index"] for t in probe["audio_tracks"]]
    selected = req.selected_audio_tracks or valid_streams
    selected = [int(x) for x in selected if int(x) in valid_streams]

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
        cmd += ["-map", "0:v:0?", "-c:v", "libx264", "-preset", "fast", "-crf", "18"]
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
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {(result.stderr or '').strip()[:800]}")

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
        clips.append({
            "name": name,
            "path": path,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "meta": meta,
        })

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
>>>>>>> 34ff7e19720a3b7f1cb7ae3d7ff524fe80832089
