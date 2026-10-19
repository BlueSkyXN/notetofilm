#!/usr/bin/env python3
"""Create an OFFLINE synthetic media fixture, optionally rendering an MP4.

No AI generation, TTS service, or company API is called. Audio consists of test
tones. Subtitle cue times are authored for those tones, not inferred speech.
"""

import argparse
import json
import math
import struct
import sys
import wave
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from notetofilm_local.compose import compose
from notetofilm_local.media import run_media_command


def write_image(path, width=640, height=360):
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = (225, 234, 240) if (x // 80 + y // 60) % 2 else (247, 241, 226)
            if 20 <= x < width - 20 and 25 <= y < 90:
                color = (55, 86, 107)
            row.extend(color)
        rows.append(b"\0" + bytes(row))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"tEXt", b"Description\0OFFLINE FIXTURE: algorithmic color grid; not AI generated")
    data += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    Path(path).write_bytes(data)


def write_tone(path, duration=1.2, frequency=440):
    samples = bytearray()
    for index in range(round(duration * 48000)):
        # Fade the ends to avoid clicks; this is still an unmistakable test tone.
        t = index / 48000
        envelope = min(1, t / 0.02, max(0, (duration - t) / 0.02))
        samples.extend(struct.pack("<h", int(5500 * envelope * math.sin(2 * math.pi * frequency * t))))
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(samples)


def create_fixture(root, render=False):
    root = Path(root).expanduser().resolve()
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if (assets / "fixture-image.png").exists():
        raise ValueError("Choose a new fixture directory; existing source files are preserved.")
    write_image(assets / "fixture-image.png")
    write_tone(assets / "fixture-tone-a.wav", 1.2, 440)
    write_tone(assets / "fixture-tone-b.wav", 1.0, 660)
    run_media_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
        "-filter_threads", "1", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
        "-vf", "drawtext=text='FIXTURE VIDEO':fontsize=32:fontcolor=white:x=20:y=20:box=1:boxcolor=black@0.8",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "1",
        str(assets / "fixture-video.mp4"),
    ])
    (assets / "fixture-captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,200\nOFFLINE FIXTURE: test tone, not speech\n", encoding="utf-8")
    manifest = {
        "output": {"width": 640, "height": 360, "fps": 15, "subtitles_mode": "burn", "subtitle_font_size": 18},
        "scenes": [
            {"id": "fixture_still_narration", "visuals": [{"path": "assets/fixture-image.png"}],
             "audio_policy": "edge_tts", "audio": {"path": "assets/fixture-tone-a.wav", "start_seconds": 0.2},
             "subtitles_path": "assets/fixture-captions.srt"},
            {"id": "fixture_native_hold", "visuals": [{"path": "assets/fixture-video.mp4", "trim_start": 0.1, "hold": True}],
             "duration_seconds": 1.2, "audio_policy": "native"},
            {"id": "fixture_mix", "visuals": [
                {"path": "assets/fixture-image.png", "duration_seconds": 0.4},
                {"path": "assets/fixture-video.mp4", "trim_start": 0.1, "duration_seconds": 0.8}],
             "audio_policy": "mix", "audio": {"path": "assets/fixture-tone-b.wav", "start_seconds": 0.1}},
            {"id": "fixture_silent", "visuals": [{"path": "assets/fixture-video.mp4", "duration_seconds": 0.8}],
             "audio_policy": "silent"},
        ],
    }
    (root / "compose.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (root / "FIXTURE.json").write_text(json.dumps({
        "fixture": True, "image_source": "algorithmic PNG", "video_source": "FFmpeg testsrc2",
        "audio_source": "synthetic sine tones; NOT EdgeTTS and NOT human speech",
        "subtitle_source": "explicitly authored fixture timing cues",
        "note": "audio_policy=edge_tts exercises the independent-narration mixing path; it does not claim service synthesis",
        "network_calls": 0,
    }, indent=2) + "\n", encoding="utf-8")
    result = compose(root, manifest, root / "output", render=render)
    return {"fixture": True, "project_root": str(root), "manifest_path": str(root / "compose.json"), **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New, empty fixture project directory")
    parser.add_argument("--render", action="store_true", help="Render the synthetic sample with real FFmpeg")
    args = parser.parse_args()
    print(json.dumps(create_fixture(args.output, args.render), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
