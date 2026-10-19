"""Render project-local images/video and independent audio with FFmpeg.

Inputs are never changed. Intermediate MOV files use PCM audio so that joining
many scenes does not accumulate independent AAC encoder padding. AAC encoding
happens once, when the final MP4 is written. Transitions are explicit cuts.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from fractions import Fraction
from pathlib import Path

from .errors import NTFError
from .media import probe, project_path, read_subtitles, run_media_command, subtitles_text


def _value(value, name, *, minimum=0, positive=False):
    if isinstance(value, bool):
        raise NTFError("compose_invalid", f"{name} must be a finite number.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise NTFError("compose_invalid", f"{name} must be a finite number.") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (positive and result <= 0):
        raise NTFError("compose_invalid", f"{name} is outside the supported range.")
    return result


def _seconds(value):
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _settings(manifest):
    raw = manifest.get("output", {})
    if not isinstance(raw, dict):
        raise NTFError("compose_invalid", "output must be an object.")
    width, height = raw.get("width", 1920), raw.get("height", 1080)
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 16 or value > 8192 or value % 2:
            raise NTFError("compose_invalid", f"Output {name} must be an even integer between 16 and 8192.")
    try:
        fps_fraction = Fraction(str(raw.get("fps", 30)))
        fps = float(fps_fraction)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise NTFError("compose_invalid", "Output fps must be a positive number or fraction.") from exc
    if not math.isfinite(fps) or not 1 <= fps <= 120:
        raise NTFError("compose_invalid", "Output fps must be between 1 and 120.")
    mode = raw.get("subtitles_mode", "sidecar")
    if mode not in ("sidecar", "burn", "none"):
        raise NTFError("compose_invalid", "subtitles_mode must be sidecar, burn, or none.")
    font = raw.get("subtitle_font", "sans-serif")
    if not isinstance(font, str) or not re.fullmatch(r"[\w .-]{1,100}", font):
        raise NTFError("compose_invalid", "subtitle_font must be a plain font family name.")
    font_size = _value(raw.get("subtitle_font_size", 22), "subtitle_font_size", positive=True)
    if font_size > 100:
        raise NTFError("compose_invalid", "subtitle_font_size must be at most 100 ASS units.")
    return {"width": width, "height": height, "fps": fps, "fps_expression": str(fps_fraction),
            "subtitles_mode": mode, "subtitle_font": font, "subtitle_font_size": font_size}


def _plan_scene(root, raw, settings, start, cache):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        raise NTFError("compose_invalid", "Every scene needs a nonempty string id.")
    scene_id = raw["id"]
    if raw.get("transition") not in (None, "cut"):
        raise NTFError("compose_unsupported", "This renderer supports cut transitions; remove the unsupported transition.")
    audio_raw = raw.get("audio") or {}
    if not isinstance(audio_raw, dict):
        raise NTFError("compose_invalid", f"Scene {scene_id}: audio must be an object.")
    policy = raw.get("audio_policy", "edge_tts" if audio_raw.get("path") else "silent")
    if policy not in ("edge_tts", "native", "mix", "silent"):
        raise NTFError("compose_invalid", f"Scene {scene_id}: unknown audio_policy.")

    def inspect(value):
        path = project_path(root, value)
        if str(path) not in cache:
            cache[str(path)] = probe(path)
        return path, cache[str(path)]

    audio = None
    audio_end = 0.0
    if audio_raw.get("path"):
        if policy not in ("edge_tts", "mix"):
            raise NTFError("audio_policy_conflict", f"Scene {scene_id}: native/silent must not supply a narration audio path.")
        path, info = inspect(audio_raw["path"])
        if not info["has_audio"]:
            raise NTFError("audio_missing", f"Scene {scene_id}: selected narration file has no audio.")
        duration = info.get("audio_duration_seconds") or info.get("duration_seconds")
        if not duration:
            raise NTFError("media_duration_unknown", f"Scene {scene_id}: narration duration is unavailable.")
        audio_start = _value(audio_raw.get("start_seconds", 0), "audio.start_seconds")
        volume = _value(audio_raw.get("volume", raw.get("narration_volume", 1)), "narration_volume")
        audio = {"path": str(path), "duration_seconds": duration, "start_seconds": audio_start,
                 "volume": volume, "metadata": info}
        audio_end = audio_start + duration
    elif policy in ("edge_tts", "mix"):
        raise NTFError("audio_missing", f"Scene {scene_id}: {policy} requires a narration audio file.")
    explicit = raw.get("duration_seconds")
    explicit = _value(explicit, "scene.duration_seconds", positive=True) if explicit is not None else None
    tail = _value(raw.get("tail_seconds", 0), "tail_seconds")
    if explicit is not None and explicit + 1e-6 < audio_end:
        raise NTFError("narration_would_be_truncated", f"Scene {scene_id}: duration is shorter than the actual narration.",
                       {"minimum_duration_seconds": audio_end, "requested_duration_seconds": explicit})
    raw_visuals = raw.get("visuals")
    if not isinstance(raw_visuals, list) or not raw_visuals:
        raise NTFError("compose_invalid", f"Scene {scene_id}: at least one visual is required.")
    visuals = []
    for item in raw_visuals:
        if not isinstance(item, dict):
            raise NTFError("compose_invalid", "Each visual must be an object.")
        path, info = inspect(item.get("path"))
        if info["kind"] not in ("image", "video"):
            raise NTFError("visual_type_invalid", f"Scene {scene_id}: selected visual is not an image or video.")
        if item.get("kind") and item["kind"] != info["kind"]:
            raise NTFError("visual_type_invalid", "Visual kind does not match the actual file.")
        trim = _value(item.get("trim_start", 0), "trim_start")
        if info["kind"] == "image" and trim:
            raise NTFError("compose_invalid", "Static images cannot have a trim_start.")
        available = info.get("duration_seconds", 0) - trim if info["kind"] == "video" else None
        if available is not None and available <= 0:
            raise NTFError("visual_trim_invalid", f"Scene {scene_id}: trim_start is beyond the video end.")
        duration = item.get("duration_seconds")
        duration = _value(duration, "visual.duration_seconds", positive=True) if duration is not None else None
        hold = item.get("hold", False)
        if not isinstance(hold, bool) and hold != "freeze":
            raise NTFError("compose_invalid", "hold must be true, false, or 'freeze'.")
        fit = item.get("fit", "contain")
        if fit not in ("contain", "cover"):
            raise NTFError("compose_invalid", "Visual fit must be contain or cover.")
        visuals.append({"path": str(path), "kind": info["kind"], "metadata": info, "trim_start": trim,
                        "available_seconds": available, "requested_duration_seconds": duration,
                        "fit": fit, "explicit_hold": hold is True or hold == "freeze"})
    if policy == "native" and not any(v["kind"] == "video" and v["metadata"]["has_audio"] for v in visuals):
        raise NTFError("native_audio_missing", f"Scene {scene_id}: no selected video contains native audio.")
    if len(visuals) == 1:
        visual = visuals[0]
        if explicit is not None:
            target = explicit
        elif visual["requested_duration_seconds"] is not None:
            target = max(visual["requested_duration_seconds"], audio_end) + tail
        elif audio:
            target = audio_end + tail
        elif visual["available_seconds"] is not None:
            target = visual["available_seconds"] + tail
        else:
            raise NTFError("duration_required", f"Scene {scene_id}: a silent still image needs an explicit duration.")
        if visual["requested_duration_seconds"] is None:
            visual["requested_duration_seconds"] = target
    else:
        for visual in visuals:
            if visual["requested_duration_seconds"] is None and visual["available_seconds"] is not None:
                visual["requested_duration_seconds"] = visual["available_seconds"]
        missing = [i for i, v in enumerate(visuals) if v["requested_duration_seconds"] is None]
        known = sum(v["requested_duration_seconds"] or 0 for v in visuals)
        target = explicit if explicit is not None else max(known, audio_end) + tail
        if missing:
            if missing != [len(visuals) - 1] or target <= known:
                raise NTFError("duration_required", f"Scene {scene_id}: specify each visual duration; only a final still may fill a known remainder.")
            visuals[-1]["requested_duration_seconds"] = target - known
    total = sum(v["requested_duration_seconds"] for v in visuals)
    if total > target + 1e-6:
        raise NTFError("scene_duration_conflict", f"Scene {scene_id}: visual durations exceed scene duration; trim the selected visuals explicitly.")
    if total < target - 1e-6:
        if not visuals[-1]["explicit_hold"]:
            raise NTFError("visual_gap_unplanned", f"Scene {scene_id}: visuals end before narration/scene; set hold:true on the final visual or add material.",
                           {"gap_seconds": target - total})
        visuals[-1]["requested_duration_seconds"] += target - total
    fps = settings["fps"]
    previous_frame = 0
    cumulative = 0.0
    for visual in visuals:
        requested = visual["requested_duration_seconds"]
        available = visual["available_seconds"]
        if available is not None and requested > available + 1e-6 and not visual["explicit_hold"]:
            raise NTFError("visual_too_short", f"Scene {scene_id}: video is shorter than required; explicitly allow a last-frame hold or add another visual.",
                           {"available_seconds": available, "required_seconds": requested})
        cumulative += requested
        end_frame = math.ceil(cumulative * fps - 1e-8)
        frames = end_frame - previous_frame
        if frames < 1:
            raise NTFError("visual_too_short", "A visual must occupy at least one output frame.")
        rendered = frames / fps
        visual.update(start_seconds=previous_frame / fps, frames=frames, duration_seconds=rendered,
                      hold_seconds=max(0.0, requested - available) if available is not None else 0.0,
                      frame_padding_seconds=max(0.0, rendered - max(requested, available)) if available is not None else 0.0)
        previous_frame = end_frame
    duration = previous_frame / fps
    native_volume = _value(raw.get("native_volume", 0.25 if policy == "mix" else 1), "native_volume")
    cues = []
    subtitle_path = None
    subtitle_offset = _value(raw.get("subtitles_offset_seconds", audio["start_seconds"] if audio else 0),
                             "subtitles_offset_seconds", minimum=None)
    if raw.get("subtitles_path"):
        subtitle_path = project_path(root, raw["subtitles_path"])
        for cue in read_subtitles(subtitle_path):
            local_start = cue["start_seconds"] + subtitle_offset
            local_end = cue["end_seconds"] + subtitle_offset
            if local_start < -1e-6 or local_end > duration + 0.001:
                raise NTFError("subtitle_out_of_range", f"Scene {scene_id}: subtitle timing lies outside the actual scene window.",
                               {"cue_start_seconds": local_start, "cue_end_seconds": local_end, "scene_duration_seconds": duration})
            cues.append({**cue, "scene_id": scene_id, "start_seconds": start + max(0, local_start),
                         "end_seconds": start + local_end})
    return {"id": scene_id, "start_seconds": start, "end_seconds": start + duration,
            "duration_seconds": duration, "requested_duration_seconds": target, "frames": previous_frame,
            "visuals": visuals, "audio": audio, "audio_policy": policy, "native_volume": native_volume,
            "subtitles_path": str(subtitle_path) if subtitle_path else None,
            "subtitles_offset_seconds": subtitle_offset, "subtitle_count": len(cues)}, cues


def _ffmpeg():
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
            "-filter_threads", "1", "-filter_complex_threads", "1"]


def _render_visual(visual, scene, settings, output):
    duration = visual["duration_seconds"]
    duration_arg = _seconds(duration)
    source_duration = min(duration, visual["available_seconds"]) if visual["kind"] == "video" else duration
    args = _ffmpeg()
    if visual["kind"] == "image":
        args += ["-loop", "1", "-framerate", settings["fps_expression"]]
    args += ["-i", visual["path"]]
    native = visual["kind"] == "video" and visual["metadata"]["has_audio"] and scene["audio_policy"] in ("native", "mix")
    if not native:
        args += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={duration_arg}"]
    width, height = settings["width"], settings["height"]
    if visual["fit"] == "contain":
        geometry = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        geometry = (f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                    f"crop={width}:{height}")
    trim = f"trim=start={_seconds(visual['trim_start'])}:duration={_seconds(source_duration)},setpts=PTS-STARTPTS"
    vf = (f"[0:v:0]setpts=PTS-STARTPTS,{trim},{geometry},setsar=1,fps={settings['fps_expression']},"
          f"tpad=stop_mode=clone:stop_duration={duration_arg},trim=duration={duration_arg},setpts=PTS-STARTPTS[v]")
    audio_source = "[0:a:0]" if native else "[1:a:0]"
    audio_trim = (f"atrim=start={_seconds(visual['trim_start'])}:duration={_seconds(source_duration)},"
                  "asetpts=PTS-STARTPTS,") if native else ""
    volume = scene["native_volume"] if native else 1
    af = (f"{audio_source}asetpts=PTS-STARTPTS,{audio_trim}aresample=48000,"
          f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,volume={_seconds(volume)},"
          f"apad,atrim=duration={duration_arg},asetpts=PTS-STARTPTS[a]")
    args += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "1",
             "-r", settings["fps_expression"], "-frames:v", str(visual["frames"]),
             "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-t", duration_arg,
             "-video_track_timescale", "90000", str(output)]
    run_media_command(args)


def _concat_list(paths, destination):
    lines = []
    for path in paths:
        relative = Path(path).relative_to(destination.parent).as_posix()
        # These are generated filenames, never caller-provided names.
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
            raise NTFError("compose_invalid", "Internal concat path is not representable.")
        lines.append(f"file '{relative}'")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_scene(scene, settings, directory):
    directory.mkdir(parents=True, exist_ok=True)
    segments = []
    for index, visual in enumerate(scene["visuals"]):
        path = directory / f"visual_{index:04d}.mov"
        _render_visual(visual, scene, settings, path)
        segments.append(path)
    picture = directory / "picture.mov"
    listing = directory / "visuals.txt"
    _concat_list(segments, listing)
    run_media_command(_ffmpeg() + ["-f", "concat", "-safe", "1", "-i", str(listing), "-map", "0:v:0",
                                    "-map", "0:a:0", "-c", "copy", "-t", _seconds(scene["duration_seconds"]), str(picture)])
    final = directory / "scene.mov"
    if scene["audio_policy"] in ("native", "silent"):
        shutil.copyfile(picture, final)
        return final
    audio = scene["audio"]
    duration = _seconds(scene["duration_seconds"])
    delay_samples = int(math.floor(audio["start_seconds"] * 48000 + 0.5))
    filters = (f"[1:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
               f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
               f"volume={_seconds(audio['volume'])},adelay=delays={delay_samples}S:all=1,"
               f"apad,atrim=duration={duration}[voice]")
    if scene["audio_policy"] == "mix":
        filters += (f";[0:a:0][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                    f"alimiter=limit=0.95:level=0:latency=1,apad,atrim=duration={duration}[a]")
    else:
        filters += ";[voice]anull[a]"
    run_media_command(_ffmpeg() + ["-i", str(picture), "-i", audio["path"], "-filter_complex", filters,
                                    "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "pcm_s16le",
                                    "-ar", "48000", "-ac", "2", "-t", duration, str(final)])
    return final


def compose(project_root: Path, manifest: dict, output_dir: Path, render: bool = True) -> dict:
    """Plan and optionally render an explicit scene manifest.

    A video may be trimmed to a known narration window. Extending past its end
    needs ``hold: true``; this holds only the final image and pads native sound
    with silence. Subtitle times are local to narration by default, or local to
    the scene for native audio; ``subtitles_offset_seconds`` overrides that offset.
    """
    root = Path(project_root).expanduser().resolve()
    output_dir = project_path(root, output_dir, must_exist=False)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("scenes"), list) or not manifest["scenes"]:
        raise NTFError("compose_invalid", "Manifest must contain a nonempty scenes list.")
    if manifest.get("transitions"):
        raise NTFError("compose_unsupported", "This renderer supports explicit cuts, not a separate transitions track.")
    settings = _settings(manifest)
    scenes, cues, cache = [], [], {}
    cursor = 0.0
    seen = set()
    for raw in manifest["scenes"]:
        scene, local_cues = _plan_scene(root, raw, settings, cursor, cache)
        if scene["id"] in seen:
            raise NTFError("compose_invalid", "Scene ids must be unique.")
        seen.add(scene["id"])
        scenes.append(scene)
        cues.extend(local_cues)
        cursor = scene["end_seconds"]
    cues.sort(key=lambda cue: (cue["start_seconds"], cue["end_seconds"]))
    video_path = output_dir / "video.mp4"
    timeline_path = output_dir / "timeline.json"
    subtitle_path = output_dir / "subtitles.srt" if cues and settings["subtitles_mode"] != "none" else None
    inputs = {Path(path) for path in cache}
    inputs.update(Path(s["subtitles_path"]) for s in scenes if s["subtitles_path"])
    for generated in (video_path, timeline_path, subtitle_path):
        if generated and generated in inputs:
            raise NTFError("output_input_collision", "Output files must not replace source media or subtitles.")
    if any(path.is_relative_to(output_dir) for path in inputs):
        raise NTFError("output_input_collision", "Use a separate render directory outside the selected source files.")
    if video_path.exists():
        raise NTFError("output_exists", "Choose a new render directory to preserve the existing MP4.")
    if output_dir.exists() and not output_dir.is_dir():
        raise NTFError("compose_invalid", "output_dir is not a directory.")
    output_dir.mkdir(parents=True, exist_ok=True)
    timeline = {"schema_version": 1, "status": "planned", "output": settings, "duration_seconds": cursor,
                "scenes": scenes, "subtitles": cues,
                "timing_source": "actual media metadata; output windows rounded up to complete frames"}
    if subtitle_path:
        subtitle_path.write_text(subtitles_text(cues), encoding="utf-8")
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {"video_path": None, "mp4_path": None, "timeline_path": str(timeline_path),
              "subtitles_path": str(subtitle_path) if subtitle_path else None,
              "duration_seconds": cursor, "scenes": scenes, "rendered": False}
    if not render:
        return result
    segment_root = output_dir / "segments"
    segment_root.mkdir(exist_ok=True)
    try:
        rendered = [_render_scene(scene, settings, segment_root / f"scene_{i:04d}")
                    for i, scene in enumerate(scenes)]
        listing = segment_root / "scenes.txt"
        _concat_list(rendered, listing)
        partial = output_dir / "video.partial.mp4"
        args = _ffmpeg() + ["-f", "concat", "-safe", "1", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0"]
        if subtitle_path and settings["subtitles_mode"] == "burn":
            style = (f"FontName={settings['subtitle_font']},FontSize={_seconds(settings['subtitle_font_size'])},"
                     "Outline=1,MarginV=16")
            args += ["-vf", f"subtitles=filename=subtitles.srt:force_style='{style}'", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "1"]
        else:
            args += ["-c:v", "copy"]
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", _seconds(cursor),
                 "-movflags", "+faststart", str(partial)]
        run_media_command(args, cwd=output_dir)
        actual = probe(partial)
        if abs(actual["duration_seconds"] - cursor) > max(0.1, 1.1 / settings["fps"]):
            raise NTFError("render_duration_mismatch", "Rendered duration differs from the planned frame timeline.",
                           {"planned_seconds": cursor, "actual_seconds": actual["duration_seconds"]})
        partial.replace(video_path)
        actual["path"] = str(video_path)
        timeline.update(status="rendered", rendered_media=actual)
        timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result.update(video_path=str(video_path), mp4_path=str(video_path), duration_seconds=actual["duration_seconds"],
                      rendered=True, media=actual)
        return result
    except NTFError as exc:
        timeline.update(status="failed", error={"code": exc.code, "message": str(exc)})
        timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise
