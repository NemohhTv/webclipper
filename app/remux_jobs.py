"""In-memory remux job tracking. Jobs run in a background thread."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from app.ffmpeg_utils import remux_mkv_to_mp4

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def create_job(paths: list[str]) -> str:
    """Start a remux job for the given MKV paths. Returns job_id."""
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "total": len(paths),
            "done": 0,
            "current_file": None,
            "results": [],
            "error": None,
        }
    def run():
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return
        for i, path in enumerate(paths):
            with _lock:
                job = _jobs.get(job_id)
                if not job:
                    return
                job["current_file"] = path
            ok, msg = remux_mkv_to_mp4(path)
            with _lock:
                job = _jobs.get(job_id)
                if not job:
                    return
                job["done"] = i + 1
                job["results"].append({"path": path, "success": ok, "message": msg})
                if not ok:
                    job["error"] = msg
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["status"] = "done" if all(r["success"] for r in job["results"]) else "failed"
                job["current_file"] = None
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        return _jobs.get(job_id)


def get_all_jobs() -> list[dict[str, Any]]:
    with _lock:
        return list(_jobs.values())
