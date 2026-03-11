"""Source management: list, add, delete. Sources are mounted folders with recordings."""
from __future__ import annotations

from app.config import load_config, save_config, SUPPORTED_EXTENSIONS
from pathlib import Path
from typing import Any


def get_sources() -> list[dict[str, str]]:
    """Return list of sources: [{ label, path }, ...]."""
    config = load_config()
    return config.get("sources", [])


def add_source(label: str, path: str) -> list[dict[str, str]]:
    """Add a source. path is container path (e.g. /mnt/recordings/Elgato)."""
    config = load_config()
    sources = list(config.get("sources", []))
    path_clean = str(Path(path).resolve())
    for s in sources:
        if (s.get("path") or "").rstrip("/") == path_clean.rstrip("/"):
            return sources
    sources.append({"label": label or Path(path).name or "Source", "path": path_clean})
    config["sources"] = sources
    save_config(config)
    return sources


def delete_source(path: str) -> list[dict[str, str]]:
    """Remove source by path."""
    config = load_config()
    path_clean = str(Path(path).resolve()).rstrip("/")
    sources = [s for s in config.get("sources", []) if (s.get("path") or "").rstrip("/") != path_clean]
    config["sources"] = sources
    save_config(config)
    return sources
