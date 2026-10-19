"""Local media inspection and subtitle utilities; no network access."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .errors import NTFError


def run_media_command(args, *, cwd=None, timeout=300):
    """Run an argv list, never a shell command."""
    if not shutil.which(str(args[0])):
        raise NTFError("media_tool_missing", f"Required executable is unavailable: {args[0]}")
    try:
        result = subprocess.run(
            [str(item) for item in args], cwd=cwd, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NTFError("media_command_timeout", "Local media processing exceeded its time limit.") from exc
    except OSError as exc:
        raise NTFError("media_command_failed", "Could not start local media processing.") from exc
    if result.returncode:
        raise NTFError(
            "media_command_failed", "Local media processing failed.",
            {"tool": str(args[0]), "returncode": result.returncode, "stderr": result.stderr[-6000:]},
        )
    return result


def _number(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _rate(value):
    try:
        rate = float(Fraction(str(value)))
        return rate if math.isfinite(rate) and rate > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def probe(path) -> dict:
    """Inspect an existing local file with ffprobe, including actual durations."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise NTFError("media_missing", "Media file does not exist.", {"path": str(path)})
    result = run_media_command(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        timeout=45,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NTFError("media_probe_invalid", "ffprobe returned invalid metadata.") from exc
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format", {})
    formats = set(fmt.get("format_name", "").split(","))
    still_formats = {
        "image2", "image2pipe", "png_pipe", "jpeg_pipe", "jpegls_pipe", "webp_pipe",
        "bmp_pipe", "tiff_pipe", "ppm_pipe", "pgm_pipe", "pam_pipe", "svg_pipe",
    }
    if video and formats.intersection(still_formats):
        kind = "image"
    elif video:
        kind = "video"
    elif audio:
        kind = "audio"
    else:
        raise NTFError("media_type_unsupported", "File contains no supported image, video, or audio stream.")
    info = {
        "path": str(path), "kind": kind, "format": fmt.get("format_name"),
        "has_audio": bool(audio), "size_bytes": path.stat().st_size,
    }
    format_duration = _number(fmt.get("duration"))
    if video:
        info.update(width=int(video.get("width", 0)), height=int(video.get("height", 0)),
                    video_codec=video.get("codec_name"), video_stream_index=video.get("index"))
        if kind == "video":
            fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
            duration = _number(video.get("duration"))
            if duration is None and fps and _number(video.get("nb_frames")):
                duration = float(video["nb_frames"]) / fps
            duration = duration if duration is not None else format_duration
            if not duration or duration <= 0:
                raise NTFError("media_duration_unknown", "Video has no readable positive duration.")
            info.update(duration_seconds=duration, fps=fps)
    if audio:
        duration = _number(audio.get("duration"))
        duration = duration if duration is not None else format_duration
        info.update(audio_codec=audio.get("codec_name"), audio_stream_index=audio.get("index"),
                    audio_duration_seconds=duration, sample_rate=_number(audio.get("sample_rate")),
                    channels=audio.get("channels"))
        if kind == "audio":
            if not duration or duration <= 0:
                raise NTFError("media_duration_unknown", "Audio has no readable positive duration.")
            info["duration_seconds"] = duration
    return info


def project_path(project_root, value, *, must_exist=True) -> Path:
    """Resolve a file within the project, including symlink targets."""
    root = Path(project_root).expanduser().resolve()
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise NTFError("media_path_invalid", "A project-local media path is required.")
    candidate = Path(value).expanduser()
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not candidate.is_relative_to(root):
        raise NTFError("path_outside_project", "Import the file into the project before using it.")
    if must_exist and not candidate.is_file():
        raise NTFError("media_missing", "Project media file does not exist.", {"path": str(candidate)})
    return candidate


_STAMP = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})$")


def parse_timestamp(value: str) -> float:
    match = _STAMP.fullmatch(value.strip())
    if not match:
        raise NTFError("subtitle_invalid", "Subtitle timestamp must use HH:MM:SS,mmm or MM:SS.mmm.")
    hours, minutes, seconds, millis = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise NTFError("subtitle_invalid", "Subtitle timestamp contains invalid minutes or seconds.")
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def read_subtitles(path) -> list[dict]:
    """Read existing SRT or simple WebVTT cues; never infer speech timings."""
    try:
        content = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    except (OSError, UnicodeError) as exc:
        raise NTFError("subtitle_unreadable", "Could not read UTF-8 subtitles.") from exc
    if not content:
        return []
    blocks = re.split(r"\n\s*\n", content)
    cues = []
    for block in blocks:
        lines = block.splitlines()
        if lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_index = next((i for i, line in enumerate(lines[:2]) if "-->" in line), None)
        if timing_index is None:
            raise NTFError("subtitle_invalid", "Subtitle cue is missing a timestamp line.")
        left, right = lines[timing_index].split("-->", 1)
        start = parse_timestamp(left)
        end = parse_timestamp(right.strip().split()[0])
        text = "\n".join(lines[timing_index + 1:]).strip()
        if end <= start or not text:
            raise NTFError("subtitle_invalid", "Subtitle cues need positive duration and nonempty text.")
        cues.append({"start_seconds": start, "end_seconds": end, "text": text})
    return cues


def timestamp(seconds) -> str:
    millis = max(0, int(math.floor(float(seconds) * 1000 + 0.5)))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def subtitles_text(cues) -> str:
    chunks = []
    for index, cue in enumerate(cues, 1):
        chunks.append(f"{index}\n{timestamp(cue['start_seconds'])} --> "
                      f"{timestamp(cue['end_seconds'])}\n{cue['text']}\n")
    return "\n".join(chunks)
