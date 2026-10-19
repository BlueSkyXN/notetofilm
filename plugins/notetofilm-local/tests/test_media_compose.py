"""Real codec integration tests using explicitly synthetic, offline fixtures."""

import copy
import hashlib
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "runtime"))
from notetofilm_local.compose import compose
from notetofilm_local.errors import NTFError
from notetofilm_local.media import probe, read_subtitles

spec = importlib.util.spec_from_file_location("media_fixture_recipe", PLUGIN / "examples/create_media_fixture.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
class MediaComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ntf-media-tests-")
        cls.root = Path(cls.temp.name)
        recipe.create_fixture(cls.root, render=False)
        cls.manifest = json.loads((cls.root / "compose.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_probe_real_image_audio_and_video(self):
        self.assertEqual(probe(self.root / "assets/fixture-image.png")["kind"], "image")
        audio = probe(self.root / "assets/fixture-tone-a.wav")
        self.assertAlmostEqual(audio["duration_seconds"], 1.2, places=3)
        video = probe(self.root / "assets/fixture-video.mp4")
        self.assertTrue(video["has_audio"])
        self.assertEqual((video["width"], video["height"]), (640, 360))

    def test_actual_mixed_render_policies_subtitles_and_source_preservation(self):
        sources = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "assets").iterdir()}
        result = compose(self.root, self.manifest, self.root / "render-test")
        info = probe(result["video_path"])
        self.assertEqual(info["video_codec"], "h264")
        self.assertEqual(info["audio_codec"], "aac")
        self.assertAlmostEqual(info["duration_seconds"], 4.6, delta=0.05)
        subtitles = read_subtitles(result["subtitles_path"])
        self.assertAlmostEqual(subtitles[0]["start_seconds"], 0.2, places=3)
        self.assertAlmostEqual(subtitles[0]["end_seconds"], 1.4, places=3)
        timeline = json.loads(Path(result["timeline_path"]).read_text())
        self.assertEqual(timeline["status"], "rendered")
        self.assertAlmostEqual(timeline["scenes"][1]["visuals"][0]["hold_seconds"], 0.3, places=3)
        def rms(start, duration):
            data = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", result["video_path"],
                                   "-t", str(duration), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                                  check=True, capture_output=True).stdout
            samples = struct.unpack("<" + "h" * (len(data) // 2), data)
            return (sum(s * s for s in samples) / len(samples)) ** 0.5
        self.assertGreater(rms(0.4, 0.3), 500)  # independent fixture narration
        self.assertGreater(rms(1.5, 0.3), 200)  # native video sound
        self.assertGreater(rms(2.8, 0.3), 500)  # mixed sound
        self.assertLess(rms(4.0, 0.3), 5)       # native sound explicitly muted
        for path, digest in sources.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_missing_explicit_hold_and_narration_truncation_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenes"][1]["visuals"][0].pop("hold")
        with self.assertRaises(NTFError) as failure:
            compose(self.root, manifest, self.root / "no-hold", render=False)
        self.assertEqual(failure.exception.code, "visual_too_short")
        manifest = copy.deepcopy(self.manifest)
        manifest["scenes"][0]["duration_seconds"] = 0.5
        with self.assertRaises(NTFError) as failure:
            compose(self.root, manifest, self.root / "truncated", render=False)
        self.assertEqual(failure.exception.code, "narration_would_be_truncated")

    def test_plan_only_real_timing_and_no_fake_subtitles(self):
        manifest = {"output": {"width": 160, "height": 90, "fps": 10}, "scenes": [{
            "id": "still", "visuals": [{"path": "assets/fixture-image.png"}], "audio_policy": "edge_tts",
            "audio": {"path": "assets/fixture-tone-a.wav"}}]}
        result = compose(self.root, manifest, self.root / "plan-test", render=False)
        self.assertIsNone(result["video_path"])
        self.assertIsNone(result["subtitles_path"])
        self.assertAlmostEqual(result["duration_seconds"], 1.2, places=3)

    def test_project_escape_and_subtitle_overflow_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenes"][0]["visuals"][0]["path"] = "/etc/passwd"
        with self.assertRaises(NTFError) as failure:
            compose(self.root, manifest, self.root / "outside", render=False)
        self.assertEqual(failure.exception.code, "path_outside_project")
        manifest = copy.deepcopy(self.manifest)
        manifest["scenes"][0]["subtitles_offset_seconds"] = 4
        with self.assertRaises(NTFError) as failure:
            compose(self.root, manifest, self.root / "bad-subtitle", render=False)
        self.assertEqual(failure.exception.code, "subtitle_out_of_range")


if __name__ == "__main__":
    unittest.main()
