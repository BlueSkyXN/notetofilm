"""Mock Edge service events plus a real MP3 fixture; no network synthesis."""

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from notetofilm_local.adapters.edge_tts import synthesize
from notetofilm_local.errors import NTFError
from notetofilm_local.media import read_subtitles


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
class EdgeAudioTests(unittest.TestCase):
    def test_real_audio_metadata_and_boundary_times_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="ntf-audio-test-") as tmp:
            root = Path(tmp)
            fixture = root / "tone.mp3"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                            "-c:a", "libmp3lame", str(fixture)], check=True)
            audio = fixture.read_bytes()
            class Communicate:
                def __init__(self, text, voice, rate, volume, pitch, boundary=None):
                    self.text = text
                async def stream(self):
                    yield {"type": "SentenceBoundary", "offset": 1000000, "duration": 4000000,
                           "text": "FIXTURE boundary"}
                    yield {"type": "audio", "data": audio}
            fake = types.SimpleNamespace(__version__="fixture", Communicate=Communicate)
            with patch("notetofilm_local.adapters.edge_tts._load_edge_tts", return_value=fake):
                result = synthesize("Fixture only", root / "generated")
            cues = read_subtitles(result["subtitles_path"])
            self.assertEqual(cues[0]["start_seconds"], 0.1)
            self.assertEqual(cues[0]["end_seconds"], 0.5)
            self.assertGreater(result["duration_seconds"], 0.79)
            boundaries = json.loads(Path(result["boundaries_path"]).read_text())
            self.assertEqual(boundaries[0]["offset"], 1000000)
            self.assertEqual(Path(result["audio_path"]).read_bytes(), audio)

    def test_provider_error_does_not_expose_query_token(self):
        class Communicate:
            def __init__(self, *args, **kwargs):
                pass
            async def stream(self):
                raise RuntimeError("https://provider.invalid/?secret=never-print-this")
                yield {}
        fake = types.SimpleNamespace(__version__="fixture", Communicate=Communicate)
        with tempfile.TemporaryDirectory(prefix="ntf-audio-fail-") as tmp:
            with patch("notetofilm_local.adapters.edge_tts._load_edge_tts", return_value=fake):
                with self.assertRaises(NTFError) as failure:
                    synthesize("Fixture", Path(tmp) / "audio")
            self.assertNotIn("never-print", json.dumps(failure.exception.as_dict()))
            self.assertFalse((Path(tmp) / "audio/audio.mp3").exists())


if __name__ == "__main__":
    unittest.main()
