"""Config loading and saving. Data lives under /data."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("WEBCLIPPER_DATA", "/data"))
CONFIG_PATH = DATA_DIR / "config.json"
CLIPS_DIR = DATA_DIR / "clips"
THUMBNAILS_DIR = DATA_DIR / "thumbnails"
PREVIEW_DIR = DATA_DIR / "preview"

DEFAULT_CONFIG = {
    "sources": [],
    "auto_refresh": False,
    "refresh_interval_seconds": 60,
    "clips_output_folder": str(CLIPS_DIR),
    "display_names": {},
}


def get_display_names() -> dict[str, str]:
    return load_config().get("display_names", {})


def set_display_name(path: str, name: str) -> None:
    c = load_config()
    names = c.get("display_names", {})
    if name:
        names[path] = name
    else:
        names.pop(path, None)
    c["display_names"] = names
    save_config(c)

SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".ts"}


def ensure_dirs() -> None:
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """Load config from disk. Returns default if missing."""
    ensure_dirs()
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so new keys appear
        out = dict(DEFAULT_CONFIG)
        out.update(data)
        return out
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist config to disk."""
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
