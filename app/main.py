"""FastAPI app: config, sources, recordings, preview, clips, remux jobs, folder browser."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import hashlib
import shutil

from fastapi import APIRouter, BackgroundTasks, HTTPException
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
    # URL-encode path so + and other chars in filenames don't break the query string
    return {"strategy": strategy, "url": f"/api/stream/preview?path={quote(path, safe='/')}", "path": preview_path}


@router.delete("/api/recordings")
def api_delete_recordings(body: DeleteRecordingsBody):
    deleted = recordings.delete_recordings(body.paths)
    for path in deleted:
        _remove_clip_from_cache(path)
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


def _clip_by_path(path: str) -> dict | None:
    """Return clip dict if path is in list_clips()."""
    for c in clips_store.list_clips():
        if c.get("path") == path:
            return c
    return None


@router.get("/api/clips")
def api_clips():
    return {"clips": clips_store.list_clips()}


@router.get("/api/clips/info")
def api_clip_info(path: str):
    """Probe a clip file (duration, audio tracks) for the editor."""
    if not _clip_by_path(path):
        raise HTTPException(404, "Clip not found")
    probe = get_file_info(path)
    return {"probe": probe}


@router.get("/api/clips/thumbnail")
def api_clip_thumbnail(path: str):
    """Generate or serve thumbnail for a clip."""
    if not _clip_by_path(path):
        raise HTTPException(404, "Clip not found")
    from app.config import THUMBNAILS_DIR
    key = hashlib.sha256(path.encode()).hexdigest()[:24]
    thumb_path = THUMBNAILS_DIR / f"{key}.jpg"
    if not thumb_path.exists():
        config.ensure_dirs()
        generate_thumbnail(path, thumb_path, time_sec=1.0)
    if not thumb_path.exists():
        raise HTTPException(404, "Thumbnail not generated")
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.delete("/api/clips")
def api_delete_clips(body: DeleteClipsBody):
    deleted = clips_store.delete_clips(body.paths)
    for path in deleted:
        _remove_clip_from_cache(path)
    return {"deleted": deleted}


@router.delete("/api/clips/cache")
def api_clear_clip_cache():
    """Clear the clip stream cache (NAS preview cache). Frees disk space."""
    count = _clear_clip_cache()
    return {"cleared": count}


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
def api_stream_recording(path: str, background_tasks: BackgroundTasks):
    """Stream recording file directly. MKV uses preview; others on NAS are cached for repeat playback."""
    info = recordings.get_recording_by_path(path)
    if not info:
        raise HTTPException(404, "Not found")
    if Path(path).suffix.lower() == ".mkv":
        raise HTTPException(
            400,
            "MKV files cannot be played directly. Use Remux to convert to MP4, or wait for the preview cache to be ready.",
        )
    path_obj = Path(path)
    try:
        is_local = path_obj.resolve().is_relative_to(config.DATA_DIR.resolve())
    except (ValueError, OSError):
        is_local = False
    if is_local:
        return FileResponse(path, media_type="video/mp4")
    cache_path = _clip_cache_path(path)
    if cache_path.exists():
        return FileResponse(cache_path, media_type="video/mp4")
    background_tasks.add_task(_copy_clip_to_cache, path, cache_path)
    return FileResponse(path, media_type="video/mp4")


def _clip_cache_path(source_path: str) -> Path:
    """Path in clip cache for a given source path (e.g. on NAS)."""
    key = hashlib.sha256(source_path.encode()).hexdigest()[:32]
    return config.CLIP_CACHE_DIR / f"{key}.mp4"


def _copy_clip_to_cache(source_path: str, cache_path: Path) -> None:
    """Background: copy clip from NAS/source to local cache for faster repeat streams."""
    try:
        if not Path(source_path).is_file():
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, cache_path)
    except (OSError, IOError):
        pass


def _remove_clip_from_cache(source_path: str) -> None:
    """Remove cached file for a clip when the clip is deleted (keeps cache lightweight)."""
    try:
        cache_path = _clip_cache_path(source_path)
        if cache_path.exists():
            cache_path.unlink()
    except (OSError, IOError):
        pass


def _clear_clip_cache() -> int:
    """Delete all files in clip cache. Returns number of files removed."""
    count = 0
    if not config.CLIP_CACHE_DIR.exists():
        return 0
    try:
        for f in config.CLIP_CACHE_DIR.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    except (OSError, IOError):
        pass
    return count


@router.get("/api/stream/clip")
def api_stream_clip(path: str, background_tasks: BackgroundTasks):
    """Stream a clip file. Clips on NAS are cached locally after first stream for faster repeat previews."""
    clips = clips_store.list_clips()
    if not any(c["path"] == path for c in clips):
        raise HTTPException(404, "Clip not found")
    path_obj = Path(path)
    try:
        is_local = path_obj.resolve().is_relative_to(config.DATA_DIR.resolve())
    except (ValueError, OSError):
        is_local = False
    if is_local:
        return FileResponse(path, media_type="video/mp4")
    cache_path = _clip_cache_path(path)
    if cache_path.exists():
        return FileResponse(cache_path, media_type="video/mp4")
    background_tasks.add_task(_copy_clip_to_cache, path, cache_path)
    return FileResponse(path, media_type="video/mp4")


@router.get("/api/clips/download")
def api_download_clip(path: str):
    """Download a clip file (Content-Disposition: attachment)."""
    clips = clips_store.list_clips()
    if not any(c["path"] == path for c in clips):
        raise HTTPException(404, "Clip not found")
    path_obj = Path(path)
    filename = path_obj.name or "clip.mp4"
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename,
    )


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
