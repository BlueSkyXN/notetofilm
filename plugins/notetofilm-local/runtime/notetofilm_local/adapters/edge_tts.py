"""Optional EdgeTTS synthesis with real returned boundary metadata.

This module makes network calls only when synthesize/synthesize_async is called.
It does not synthesize placeholder speech or estimate subtitle timing by length.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ..errors import NTFError
from ..media import probe, subtitles_text


def _load_edge_tts():
    try:
        package = importlib.import_module("edge_tts")
    except ImportError as exc:
        raise NTFError("audio_dependency_missing", "Install the optional audio dependency: edge-tts==7.2.8.") from exc
    # Same narrowly versioned compatibility fix as the parent repository's
    # app/tts_compat.py. It is initialized before use, never changed per request.
    if getattr(package, "__version__", None) == "7.2.8":
        communication = importlib.import_module("edge_tts.communicate")
        constants = importlib.import_module("edge_tts.constants")
        if urlsplit(communication.WSS_URL).hostname == "speech.platform.bing.com":
            communication.WSS_URL = (
                "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1?"
                + urlencode({"Ocp-Apim-Subscription-Key": constants.TRUSTED_CLIENT_TOKEN})
            )
    return package


def _boundary_cues(boundaries):
    """Use actual service offsets; group word events without invented timings."""
    cues = []
    current = None
    for boundary in boundaries:
        start = boundary["offset"] / 10_000_000
        end = (boundary["offset"] + boundary["duration"]) / 10_000_000
        text = boundary["text"].strip()
        if not text or end <= start:
            continue
        if boundary["type"] == "SentenceBoundary":
            if current:
                cues.append(current)
                current = None
            cues.append({"start_seconds": start, "end_seconds": end, "text": text})
            continue
        if current and (start - current["end_seconds"] > 0.35 or len(current["text"]) >= 42):
            cues.append(current)
            current = None
        if current is None:
            current = {"start_seconds": start, "end_seconds": end, "text": text}
        else:
            separator = " " if re.search(r"[A-Za-z0-9]$", current["text"]) and re.match(r"[A-Za-z0-9]", text) else ""
            current["text"] += separator + text
            current["end_seconds"] = end
        if re.search(r"[。！？.!?;；]$", text):
            cues.append(current)
            current = None
    if current:
        cues.append(current)
    return cues


async def synthesize_async(text, output_dir, voice="zh-CN-XiaoxiaoNeural", rate="+0%", volume="+0%",
                           pitch="+0Hz", speech_text=None):
    if not isinstance(text, str) or not text.strip():
        raise NTFError("audio_text_required", "Provide nonempty narration text.")
    spoken = text if speech_text is None else speech_text
    if not isinstance(spoken, str) or not spoken.strip():
        raise NTFError("audio_text_required", "speech_text must be nonempty when provided.")
    package = _load_edge_tts()
    directory = Path(output_dir).expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise NTFError("audio_output_invalid", "Audio output must be a directory.")
    directory.mkdir(parents=True, exist_ok=True)
    audio_path = directory / "audio.mp3"
    boundaries_path = directory / "boundaries.json"
    subtitles_path = directory / "subtitles.srt"
    metadata_path = directory / "audio.json"
    if any(path.exists() for path in (audio_path, boundaries_path, subtitles_path, metadata_path)):
        raise NTFError("output_exists", "Choose a new audio output directory to preserve existing media.")
    partial = directory / "audio.mp3.part"
    kwargs = {"voice": voice, "rate": rate, "volume": volume, "pitch": pitch}
    if os.environ.get("NTF_EDGE_PROXY"):
        kwargs["proxy"] = os.environ["NTF_EDGE_PROXY"]
    # Sentence-boundary support differs across package versions. Avoid passing
    # an unsupported keyword to older versions used by an existing client.
    try:
        if "boundary" in inspect.signature(package.Communicate).parameters:
            kwargs["boundary"] = "SentenceBoundary"
    except (TypeError, ValueError):
        pass
    boundaries = []
    audio_bytes = 0
    try:
        communication = package.Communicate(spoken, **kwargs)
        async with asyncio.timeout(300):
            with partial.open("wb") as destination:
                async for chunk in communication.stream():
                    if chunk.get("type") == "audio":
                        data = chunk.get("data")
                        if not isinstance(data, bytes):
                            raise NTFError("audio_response_invalid", "EdgeTTS returned an invalid audio chunk.")
                        destination.write(data)
                        audio_bytes += len(data)
                    elif chunk.get("type") in ("WordBoundary", "SentenceBoundary"):
                        offset, duration = chunk.get("offset"), chunk.get("duration")
                        if (not isinstance(offset, (int, float)) or isinstance(offset, bool)
                                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                                or not math.isfinite(offset) or not math.isfinite(duration)
                                or offset < 0 or duration < 0 or not isinstance(chunk.get("text"), str)):
                            raise NTFError("audio_response_invalid", "EdgeTTS returned invalid boundary metadata.")
                        boundaries.append({"type": chunk["type"], "offset": offset, "duration": duration,
                                           "text": chunk["text"]})
        if not audio_bytes:
            raise NTFError("audio_empty", "EdgeTTS returned no audio.")
        information = probe(partial)
        if information["kind"] != "audio":
            raise NTFError("audio_response_invalid", "EdgeTTS response is not an audio file.")
        cues = _boundary_cues(boundaries)
        duration_seconds = information["duration_seconds"]
        warnings = []
        if not cues:
            warnings.append("The service returned no usable subtitle boundaries; no subtitle timings were fabricated.")
        elif max(cue["end_seconds"] for cue in cues) > duration_seconds + 0.1:
            warnings.append("Returned subtitle boundaries extend beyond the probed audio duration; retain the metadata and align before composition.")
        metadata = {
            "engine": "edge-tts", "engine_version": getattr(package, "__version__", "unknown"),
            "text": text, "speech_text": spoken, "voice": voice, "rate": rate, "volume": volume, "pitch": pitch,
            "duration_seconds": duration_seconds, "audio_bytes": audio_bytes,
            "boundary_time_unit": "100 nanoseconds", "subtitle_text_source": "service boundary text",
            "warnings": warnings,
        }
        boundaries_path.write_text(json.dumps(boundaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if cues:
            subtitles_path.write_text(subtitles_text(cues), encoding="utf-8")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        partial.replace(audio_path)
        return {"files": [{"path": str(audio_path), "kind": "audio"}], "audio_path": str(audio_path),
                "duration_seconds": duration_seconds, "boundaries_path": str(boundaries_path),
                "subtitles_path": str(subtitles_path) if cues else None, "metadata_path": str(metadata_path),
                "voice": voice, "warnings": warnings}
    except NTFError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        # Provider exception strings can include the endpoint query token.
        raise NTFError("audio_synthesis_failed", "EdgeTTS synthesis failed; existing project media was preserved.",
                       {"exception_type": type(exc).__name__}) from exc


def synthesize(text, output_dir, voice="zh-CN-XiaoxiaoNeural", rate="+0%", volume="+0%", pitch="+0Hz", speech_text=None):
    """Synchronous CLI wrapper; asynchronous callers can await synthesize_async."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(synthesize_async(text, output_dir, voice, rate, volume, pitch, speech_text))
    raise NTFError("audio_async_context", "Await synthesize_async when calling from an active event loop.")
