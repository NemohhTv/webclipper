"""Recordings scanning and file info. Directory-based scan for supported video extensions."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.config import SUPPORTED_EXTENSIONS, THUMBNAILS_DIR
from app.sources import get_sources
from app.ffmpeg_utils import get_file_info as ffprobe_file_info


def scan_recordings(source_filter: str | None = None) -> list[dict[str, Any]]:
    """
    Scan all source folders (or filtered) and return list of recording entries.
    Each entry: path, name, source_label, size_bytes, modified_ts, extension, size_human.
    """
    sources = get_sources()
    if source_filter:
        sources = [s for s in sources if (s.get("path") or "").rstrip("/") == source_filter.rstrip("/")]
    results: list[dict[str, Any]] = []
    for src in sources:
        label = src.get("label") or "Unknown"
        base = Path(src.get("path") or "")
        if not base.exists():
            continue
        try:
            for entry in base.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                size = stat.st_size
                results.append({
                    "path": str(entry.resolve()),
                    "name": entry.name,
                    "source_label": label,
                    "size_bytes": size,
                    "size_human": _human_size(size),
                    "modified_ts": stat.st_mtime,
                    "modified_iso": _ts_to_iso(stat.st_mtime),
                    "extension": entry.suffix.lower().lstrip("."),
                })
        except (PermissionError, OSError):
            continue
    # Sort by modified desc (newest first)
    results.sort(key=lambda x: x["modified_ts"], reverse=True)
    return results


def _human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def _ts_to_iso(ts: float) -> str:
    from datetime import datetime
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_recording_by_path(path: str) -> dict[str, Any] | None:
    """Return one recording dict if path is under a known source."""
    path_abs = str(Path(path).resolve())
    for src in get_sources():
        base = Path(src.get("path") or "")
        try:
            if Path(path_abs).is_relative_to(base):
                p = Path(path_abs)
                if not p.is_file():
                    return None
                stat = p.stat()
                return {
                    "path": path_abs,
                    "name": p.name,
                    "source_label": src.get("label") or "Unknown",
                    "size_bytes": stat.st_size,
                    "size_human": _human_size(stat.st_size),
                    "modified_ts": stat.st_mtime,
                    "modified_iso": _ts_to_iso(stat.st_mtime),
                    "extension": p.suffix.lower().lstrip("."),
                }
        except (ValueError, OSError):
            continue
    return None


def delete_recordings(paths: list[str]) -> list[str]:
    """Delete files by path. Returns list of deleted paths (or errors)."""
    deleted = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            p.unlink()
            deleted.append(path)
        except OSError:
            pass
    return deleted


def thumbnail_path_for_file(file_path: str) -> Path:
    """Path where thumbnail for this recording should be stored."""
    import hashlib
    key = hashlib.sha256(file_path.encode()).hexdigest()[:24]
    ext = Path(file_path).suffix.lower()
    return THUMBNAILS_DIR / f"{key}{ext}.jpg"
