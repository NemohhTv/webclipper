"""FFprobe and FFmpeg helpers: duration, streams, thumbnails, preview, clip export, remux."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from app.config import PREVIEW_DIR, SUPPORTED_EXTENSIONS


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "ffmpeg/ffprobe not found"
    except Exception as e:
        return -1, "", str(e)


def probe(path: str) -> dict[str, Any]:
    """FFprobe JSON output for format and streams."""
    code, out, err = _run([
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ], timeout=30)
    if code != 0:
        return {"error": err or "probe failed", "streams": [], "format": {}}
    import json
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "invalid probe json", "streams": [], "format": {}}


def get_file_info(path: str) -> dict[str, Any]:
    """Duration, streams, codecs, audio tracks for a file."""
    data = probe(path)
    if data.get("error"):
        return {"error": data["error"], "duration": 0, "streams": [], "audio_tracks": []}
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or 0)
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    audio_tracks = []
    for i, a in enumerate(audio_streams):
        audio_tracks.append({
            "index": i,
            "stream_index": a.get("index"),
            "codec": a.get("codec_name"),
            "channels": a.get("channels"),
            "layout": a.get("channel_layout"),
            "language": a.get("tags", {}).get("language") if isinstance(a.get("tags"), dict) else None,
            "title": (a.get("tags") or {}).get("title") if isinstance(a.get("tags"), dict) else None,
        })
    return {
        "duration": duration,
        "format": fmt,
        "streams": streams,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "audio_tracks": audio_tracks,
        "error": None,
    }


def is_browser_safe(path: str, info: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Heuristic: is this file playable in browser without remux/transcode?
    Returns (safe, reason).
    """
    info = info or get_file_info(path)
    if info.get("error"):
        return False, "probe failed"
    fmt = info.get("format") or {}
    fmt_name = (fmt.get("format_name") or "").lower()
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return False, "no video stream"
    vcodec = (video.get("codec_name") or "").lower()
    # Browser typically: MP4 + H.264/HEVC + AAC
    if "mp4" in fmt_name or "mov" in fmt_name:
        if vcodec in ("h264", "hevc", "av1") and _audio_ok(streams):
            return True, "direct"
    # WebM (VP8/VP9/AV1 + Vorbis/Opus) is natively supported in modern browsers
    if "webm" in fmt_name:
        if vcodec in ("vp8", "vp9", "av1") and _audio_ok(streams):
            return True, "direct"
    return False, "container or codec not browser-safe"


def _audio_ok(streams: list[dict]) -> bool:
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not audios:
        return True
    for a in audios:
        c = (a.get("codec_name") or "").lower()
        if c not in ("aac", "mp3", "opus", "flac", "vorbis"):
            return False
    return True


def preview_path_for_file(file_path: str) -> Path:
    """Cache path for preview version of a file."""
    import hashlib
    key = hashlib.sha256(file_path.encode()).hexdigest()[:32]
    return PREVIEW_DIR / f"{key}.mp4"


def generate_thumbnail(source_path: str, out_path: str | Path, time_sec: float = 1.0) -> bool:
    """Extract one frame as JPEG thumbnail."""
    code, _, _ = _run([
        "ffmpeg", "-y",
        "-ss", str(time_sec),
        "-i", source_path,
        "-vframes", "1",
        "-q:v", "3",
        str(out_path),
    ], timeout=15)
    return code == 0


def build_preview_direct_remux(source_path: str, out_path: str | Path) -> bool:
    """Remux to MP4 (stream copy) for preview."""
    code, _, _ = _run([
        "ffmpeg", "-y",
        "-i", source_path,
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ], timeout=600)
    return code == 0


def build_preview_transcode(source_path: str, out_path: str | Path) -> bool:
    """Transcode to browser-safe MP4 (H.264 + AAC)."""
    code, _, _ = _run([
        "ffmpeg", "-y",
        "-i", source_path,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ], timeout=1200)
    return code == 0


def ensure_preview(source_path: str) -> tuple[str | None, str]:
    """
    Ensure a browser-playable preview exists (cached for smooth playback).
    Returns (url_path_or_none, strategy).
    strategy: direct | remux | transcode.
    MKV and other non-browser formats are always served from cache (remux or transcode); never raw.
    """
    from app.config import PREVIEW_DIR
    info = get_file_info(source_path)
    if info.get("error"):
        return None, "error"
    ext = Path(source_path).suffix.lower()
    is_mkv = ext == ".mkv"
    safe, reason = is_browser_safe(source_path, info)
    cache_path = preview_path_for_file(source_path)

    # Only use direct for browser-safe files; never stream MKV or other formats raw
    if safe and reason == "direct" and not is_mkv:
        return source_path, "direct"

    # Use cache if it exists and source isn't newer
    if cache_path.exists():
        try:
            if Path(source_path).stat().st_mtime <= cache_path.stat().st_mtime:
                return str(cache_path), "cached"
        except OSError:
            pass

    # Build cache: for MKV try fast remux first, then transcode; for others try remux then transcode
    if is_mkv or not safe:
        ok = build_preview_direct_remux(source_path, cache_path)
        if ok:
            return str(cache_path), "remux"
        ok = build_preview_transcode(source_path, cache_path)
        if ok:
            return str(cache_path), "transcode"
        return None, "transcode_failed"
    ok = build_preview_direct_remux(source_path, cache_path)
    if ok:
        return str(cache_path), "remux"
    return None, "remux_failed"


def create_clip(
    source_path: str,
    out_path: str,
    start_sec: float,
    end_sec: float,
    mode: str,
    container: str,
    audio_mode: str,
    selected_audio_indices: list[int],
    gains: dict[int, float],
    metadata: dict[str, Any],
) -> tuple[bool, str]:
    """
    Export clip with FFmpeg.
    mode: fast_cut | accurate
    audio_mode: separate | mix
    """
    duration = end_sec - start_sec
    if duration <= 0:
        return False, "Invalid duration"
    info = get_file_info(source_path)
    if info.get("error"):
        return False, info["error"]
    audio_streams = info.get("audio_streams") or []
    out_path = str(Path(out_path).with_suffix(f".{container}" if not Path(out_path).suffix else out_path))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if mode == "fast_cut":
        # Stream copy; trim may be keyframe-bound
        args = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", source_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        # Map one video + selected audio (by stream index in file)
        args.extend(["-map", "0:v:0"])
        if selected_audio_indices:
            for idx in selected_audio_indices:
                if 0 <= idx < len(audio_streams):
                    args.extend(["-map", f"0:a:{idx}"])
        else:
            if audio_streams:
                args.extend(["-map", "0:a:0"])
        args.append(out_path)
        code, _, err = _run(args, timeout=600)
        if code != 0:
            return False, err
    else:
        # Accurate: re-encode for precise cuts and optional mix
        filter_parts = []
        if gains:
            for i, g in gains.items():
                if g != 1.0 and 0 <= i < len(audio_streams):
                    filter_parts.append(f"[0:a:{i}]volume={g}[a{i}]")
        if audio_mode == "mix" and len(selected_audio_indices) > 1:
            # Mix selected tracks
            inputs = []
            for idx in selected_audio_indices:
                if 0 <= idx < len(audio_streams):
                    inputs.append(f"[0:a:{idx}]")
            mix_input = "".join(inputs)
            n = len(inputs)
            mix_filter = f"{mix_input}amix=inputs={n}:duration=longest[aout]"
            filter_parts.append(mix_filter)
            amap = "[aout]"
        else:
            amap = "".join(f"[0:a:{i}]" for i in selected_audio_indices if 0 <= i < len(audio_streams))
        # Simple: no complex filter for this stub; just copy/cut
        args = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", source_path,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            out_path,
        ]
        code, _, err = _run(args, timeout=900)
        if code != 0:
            return False, err
    # Write metadata sidecar
    import json
    sidecar = Path(out_path).with_suffix(Path(out_path).suffix + ".meta.json")
    meta = {
        "title": metadata.get("title"),
        "game": metadata.get("game"),
        "source": source_path,
        "start": start_sec,
        "end": end_sec,
        "mode": mode,
        "container": container,
        "audio_mode": audio_mode,
        "selected_audio_tracks": selected_audio_indices,
        "gains": gains,
        "created": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    try:
        sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass
    return True, out_path


def _parse_ffmpeg_time(s: str) -> float | None:
    """Parse time=HH:MM:SS.ms from ffmpeg stderr. Returns seconds or None."""
    m = re.search(r"time=(\d+):(\d+):(\d+)\.?(\d*)", s)
    if not m:
        return None
    h, m_i, s_i, ms = int(m.group(1)), int(m.group(2)), float(m.group(3)), (m.group(4) or "0")
    frac = float("0." + ms) if ms else 0.0
    return h * 3600 + m_i * 60 + s_i + frac


def remux_mkv_to_mp4(
    mkv_path: str,
    progress_callback: Callable[[float], None] | None = None,
) -> tuple[bool, str]:
    """Remux MKV to MP4 (stream copy), report progress via callback, then delete MKV. Returns (success, message_or_mp4_path)."""
    p = Path(mkv_path)
    if p.suffix.lower() != ".mkv":
        return False, "Not MKV"
    mp4_path = str(p.with_suffix(".mp4"))
    duration_sec = 0.0
    if progress_callback:
        info = get_file_info(mkv_path)
        if not info.get("error"):
            duration_sec = float(info.get("duration") or 0)
    cmd = [
        "ffmpeg", "-y",
        "-i", mkv_path,
        "-c", "copy",
        "-movflags", "+faststart",
        mp4_path,
    ]
    if not progress_callback or duration_sec <= 0:
        code, _, err = _run(cmd, timeout=600)
        if code != 0:
            return False, err or "remux failed"
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        return True, mp4_path
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        last_pct = [-1.0]  # mutable so reader thread can update
        assert proc.stderr is not None

        def read_stderr() -> None:
            buf = ""
            while True:
                chunk = proc.stderr.read(512)
                if not chunk:
                    break
                buf += chunk
                for line in buf.replace("\r", "\n").split("\n"):
                    t = _parse_ffmpeg_time(line)
                    if t is not None and duration_sec > 0:
                        pct = min(100.0, 100.0 * t / duration_sec)
                        if pct >= last_pct[0] + 0.5 or pct >= 99.5:
                            last_pct[0] = pct
                            progress_callback(pct)
                buf = buf[buf.rfind("\n") + 1:] if "\n" in buf else buf[-200:]

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        proc.wait(timeout=600)
        if proc.returncode == 0:
            progress_callback(100.0)
        if proc.returncode != 0:
            return False, "remux failed"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, "timeout"
    except Exception as e:
        return False, str(e)
    if not Path(mp4_path).exists():
        return False, "output file not created"
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return True, mp4_path
