"""Clips library: list, create, delete, metadata."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import load_config, CLIPS_DIR


def get_clips_dir() -> Path:
    config = load_config()
    folder = config.get("clips_output_folder") or str(CLIPS_DIR)
    return Path(folder)


def list_clips() -> list[dict[str, Any]]:
    """List clip files and their metadata from the clips folder."""
    base = get_clips_dir()
    if not base.exists():
        return []
    results = []
    for f in base.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() in (".json", ".meta.json"):
            continue
        if f.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        meta = {}
        sidecar = f.with_suffix(f.suffix + ".meta.json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        size = stat.st_size
        results.append({
            "path": str(f.resolve()),
            "name": f.name,
            "size_bytes": size,
            "size_human": _human(size),
            "modified_ts": stat.st_mtime,
            "modified_iso": _ts_iso(stat.st_mtime),
            "title": meta.get("title") or f.stem,
            "game": meta.get("game"),
            "mode": meta.get("mode"),
        })
    results.sort(key=lambda x: x["modified_ts"], reverse=True)
    return results


def _human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def _ts_iso(ts: float) -> str:
    from datetime import datetime
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def delete_clips(paths: list[str]) -> list[str]:
    """Delete clip files and their sidecars. Returns deleted paths."""
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
        sidecar = p.with_suffix(p.suffix + ".meta.json")
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
    return deleted
