"""Deterministic protocol fixtures only: no company or paid generation calls."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from notetofilm_local.adapters import get_adapter
from notetofilm_local.capabilities import check_request, request_parameters
from notetofilm_local.config import default_config, list_routes, load_config, resolve_route
from notetofilm_local.errors import NTFError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
VIDEO_CONTAINER_FIXTURE = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"  # header fixture, not a real video


class Reply(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode() if isinstance(value, dict) else value)


class MockHTTP:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply)


def route(adapter, modality):
    modes = ["text_to_image", "reference_to_image"] if modality == "image" else ["text_to_video", "image_to_video", "reference_to_video", "first_last_to_video"]
    params = {
        "count": {"type": "integer", "minimum": 1, "maximum": 4},
        "size": {"type": "string"}, "quality": {"type": "string"},
        "duration_seconds": {"type": "number", "minimum": 1, "maximum": 20},
        "aspect_ratio": {"enum": ["16:9", "9:16"]}, "resolution": {"enum": ["720p", "1080p"]},
        "generate_audio": {"type": "boolean"}, "return_last_frame": {"type": "boolean"},
        "seed": {"type": "integer"}, "input_fidelity": {"type": "string"},
    }
    return {
        "id": "fixture-route", "model": "opaque-company-model", "modality": modality, "adapter": adapter,
        "connection": {"base_url": "https://company.example/prefix", "auth": {"type": "header", "header": "X-Company-Key", "env": "NTF_TEST_TOKEN"}},
        "capabilities": {"status": "confirmed", "evidence": "unit-test fixture only", "modes": {mode: {"supported": True, "max_reference_images": 8, "parameters": copy.deepcopy(params)} for mode in modes}},
    }


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.png").write_bytes(PNG)
        (self.root / "b.png").write_bytes(PNG + b"second fixture")
        self.env = patch.dict(os.environ, {"NTF_TEST_TOKEN": "fixture-secret-never-output"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def adapter(self, name, modality, *replies):
        adapter = get_adapter(route(name, modality))
        adapter.opener = MockHTTP(*replies)
        return adapter

    def assert_code(self, code, fn):
        with self.assertRaises(NTFError) as error:
            fn()
        self.assertEqual(error.exception.code, code)
        self.assertNotIn("fixture-secret-never-output", str(error.exception.as_dict()))
        return error.exception

    def test_six_exact_models_default_unconfirmed_and_no_inference(self):
        config = default_config()
        self.assertEqual({r["model"] for r in config["routes"].values()}, {
            "veo-3.1-generate-preview", "doubao-seedance-2-5", "doubao-seedance-2-0-260128",
            "gpt-image-2.5-sunburst", "gpt-image-2", "gpt-image-2.5-flare"})
        self.assertEqual(len(list_routes(config)), 6)
        for item in list_routes(config):
            self.assertIsNone(item["adapter"])
            self.assertEqual(item["capability_status"], "unknown")
        selected = resolve_route(config, None, "image")
        self.assert_code("adapter_unconfigured", lambda: get_adapter(selected))
        self.assert_code("capability_unknown", lambda: check_request(selected, {"prompt": "fixture"}))

    def test_route_snapshot_pins_company_url_without_secret_values(self):
        config = default_config()
        with patch.dict(os.environ, {"NTF_COMPANY_API_BASE_URL": "https://first.example/v1", "NTF_COMPANY_API_KEY": "do-not-copy"}):
            selected = resolve_route(config, None, "video")
        with patch.dict(os.environ, {"NTF_COMPANY_API_BASE_URL": "https://other.example/v1"}):
            self.assertEqual(selected["connection"]["base_url"], "https://first.example/v1")
        self.assertEqual(selected["connection"]["auth"]["env"], "NTF_COMPANY_API_KEY")
        self.assertNotIn("do-not-copy", json.dumps(selected))

    def test_relative_capability_file_and_inline_secret_rejection(self):
        config = default_config()
        profile = {"status": "unknown", "modes": {}}
        (self.root / "cap.json").write_text(json.dumps(profile))
        config["routes"]["company-veo"]["capabilities"] = "cap.json"
        path = self.root / "config.json"
        path.write_text(json.dumps(config))
        self.assertEqual(load_config(path)["routes"]["company-veo"]["capabilities"], profile)
        config["connections"]["company"]["api_key"] = "sensitive"
        path.write_text(json.dumps(config))
        self.assert_code("config_credentials", lambda: load_config(path))

    def test_no_credential_in_base_url(self):
        config = default_config()
        config["connections"]["company"]["base_url"] = "https://user:secret@company.example"
        self.assert_code("connection_invalid", lambda: resolve_route(config, None, "image"))

    def test_generation_json_and_configurable_path(self):
        result = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        adapter = self.adapter("openai-images", "image", result)
        adapter.route["paths"] = {"generations": "custom/generate"}
        answer = adapter.submit({"prompt": "test", "output": {"count": 1, "size": "1024x1024"}}, self.root)
        sent = adapter.opener.requests[0]
        self.assertEqual(sent.full_url, "https://company.example/prefix/custom/generate")
        self.assertEqual(json.loads(sent.data), {"model": "opaque-company-model", "prompt": "test", "n": 1, "size": "1024x1024"})
        files = adapter.fetch(answer, self.root / "out")
        self.assertEqual(Path(files[0]["path"]).read_bytes(), PNG)

    def test_additional_header_envs_are_sent_without_primary_auth(self):
        adapter = self.adapter("openai-images", "image", {"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        adapter.connection["auth"] = {"type": "none"}
        adapter.connection["header_envs"] = {"X-Company-Workspace": "NTF_TEST_TOKEN"}
        adapter.submit({"prompt": "test"}, self.root)
        sent = adapter.opener.requests[0]
        self.assertEqual(sent.get_header("X-company-workspace"), "fixture-secret-never-output")
        self.assertIsNone(sent.get_header("Authorization"))

    def test_multi_reference_order_and_all_candidates_preserved(self):
        b64 = base64.b64encode(PNG).decode()
        adapter = self.adapter("openai-images", "image", {"data": [{"b64_json": b64}, {"b64_json": b64}]})
        request = {"prompt": "preserve references", "mode": "reference_to_image", "references": [
            {"path": "b.png", "position": 1, "role": "subject", "instruction": "use figure"},
            {"path": "a.png", "position": 2, "role": "style", "instruction": "use linework"},
        ], "output": {"count": 2}}
        answer = adapter.submit(request, self.root)
        sent = adapter.opener.requests[0]
        self.assertTrue(sent.full_url.endswith("/images/edits"))
        self.assertEqual(sent.data.count(b'name="image[]"'), 2)
        self.assertLess(sent.data.index(b"second fixture"), sent.data.index(b'filename="reference-2.png"'))
        self.assertIn(b"Reference image 2 (style): use linework", sent.data)
        self.assertEqual(len(adapter.fetch(answer, self.root / "out")), 2)

    def test_mode_does_not_silently_remove_references(self):
        adapter = self.adapter("openai-images", "image")
        self.assert_code("mode_inputs", lambda: adapter.submit({"prompt": "test", "mode": "text_to_image", "references": [{"path": "a.png"}]}, self.root))
        self.assertEqual(adapter.opener.requests, [])

    def test_reference_positions_are_not_silently_sorted(self):
        adapter = self.adapter("openai-images", "image")
        self.assert_code("reference_order", lambda: adapter.submit({"prompt": "test", "references": [{"path": "a.png", "position": 2}]}, self.root))

    def test_unknown_parameters_and_provider_mismatch_stop_before_post(self):
        adapter = self.adapter("gemini-veo", "video")
        self.assert_code("unsupported_parameter", lambda: adapter.submit({"prompt": "test", "parameters": {"generate_audio": False}}, self.root))
        self.assert_code("unsupported_parameter", lambda: adapter.submit({"prompt": "test", "magic_option": True}, self.root))
        self.assertEqual(adapter.opener.requests, [])

    def test_company_limits_and_parameter_combinations(self):
        adapter = self.adapter("openai-images", "image")
        spec = adapter.route["capabilities"]["modes"]["reference_to_image"]
        spec["max_reference_images"] = 1
        self.assert_code("reference_limit", lambda: adapter.submit({"prompt": "test", "references": [{"path": "a.png"}, {"path": "b.png"}]}, self.root))
        video = self.adapter("gemini-veo", "video")
        video.route["capabilities"]["modes"]["text_to_video"]["constraints"] = [{"when": {"resolution": "1080p"}, "require": {"duration_seconds": [8]}}]
        self.assert_code("parameter_combination", lambda: video.submit({"prompt": "test", "parameters": {"resolution": "1080p", "duration_seconds": 4}}, self.root))

    def test_paths_must_stay_inside_project_including_symlinks(self):
        adapter = self.adapter("openai-images", "image")
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / "outside.png"
            source.write_bytes(PNG)
            (self.root / "link.png").symlink_to(source)
            self.assert_code("path_outside_project", lambda: adapter.submit({"prompt": "test", "references": [{"path": "link.png"}]}, self.root))
        self.assertEqual(adapter.opener.requests, [])

    def test_post_timeout_is_unknown_and_never_retried(self):
        adapter = self.adapter("openai-images", "image", URLError(TimeoutError("request maybe accepted")))
        self.assert_code("submission_unknown", lambda: adapter.submit({"prompt": "test"}, self.root))
        self.assertEqual(len(adapter.opener.requests), 1)

    def test_post_redirect_does_not_forward_or_resubmit(self):
        error = HTTPError("https://company.example", 307, "redirect", {"Location": "https://elsewhere.example"}, None)
        adapter = self.adapter("ark-video", "video", error)
        self.assert_code("submission_unknown", lambda: adapter.submit({"prompt": "test"}, self.root))
        self.assertEqual(len(adapter.opener.requests), 1)

    def test_post_500_outcome_unknown_and_not_retried(self):
        error = HTTPError("https://company.example", 502, "error", {}, None)
        adapter = self.adapter("gemini-veo", "video", error)
        self.assert_code("submission_unknown", lambda: adapter.submit({"prompt": "test"}, self.root))
        self.assertEqual(len(adapter.opener.requests), 1)

    def test_cross_origin_download_redirect_strips_company_credentials(self):
        redirect = HTTPError("https://company.example/prefix/file", 302, "redirect", {"Location": "https://storage.example/media.png"}, None)
        adapter = self.adapter("openai-images", "image", redirect, PNG)
        answer = {"status": "succeeded", "outputs": [{"kind": "image", "url": "https://company.example/prefix/file"}]}
        adapter.fetch(answer, self.root / "out")
        first, second = adapter.opener.requests
        self.assertEqual(first.get_header("X-company-key"), "fixture-secret-never-output")
        self.assertIsNone(second.get_header("X-company-key"))
        self.assertIsNone(second.get_header("Authorization"))

    def test_direct_storage_download_never_receives_company_credentials(self):
        adapter = self.adapter("openai-images", "image", PNG)
        adapter.fetch({"status": "succeeded", "outputs": [{"kind": "image", "url": "https://storage.example/image"}]}, self.root / "out")
        self.assertIsNone(adapter.opener.requests[0].get_header("X-company-key"))

    def test_same_origin_download_keeps_explicit_header_auth(self):
        adapter = self.adapter("openai-images", "image", PNG)
        adapter.fetch({"status": "succeeded", "outputs": [{"kind": "image", "url": "https://company.example/file"}]}, self.root / "out")
        self.assertEqual(adapter.opener.requests[0].get_header("X-company-key"), "fixture-secret-never-output")

    def test_ark_first_last_payload_poll_and_fetch(self):
        adapter = self.adapter("ark-video", "video", {"id": "job-42"}, {"id": "job-42", "status": "running"}, {"id": "job-42", "status": "succeeded", "content": {"video_url": "https://store.example/v.mp4"}}, VIDEO_CONTAINER_FIXTURE)
        result = adapter.submit({"prompt": "move", "mode": "first_last_to_video", "start_frame": {"path": "a.png"}, "end_frame": {"path": "b.png"}, "parameters": {"duration_seconds": 8, "aspect_ratio": "16:9", "generate_audio": False}}, self.root)
        body = json.loads(adapter.opener.requests[0].data)
        self.assertEqual([c.get("role") for c in body["content"]], [None, "first_frame", "last_frame"])
        self.assertTrue(body["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(body["duration"], 8)
        self.assertFalse(body["generate_audio"])
        self.assertEqual(adapter.poll(result["job"])["status"], "submitted")
        done = adapter.poll(result["job"])
        files = adapter.fetch(done, self.root / "out")
        self.assertEqual(files[0]["kind"], "video")
        self.assertEqual(adapter.opener.requests[1].get_method(), "GET")
        self.assertTrue(adapter.opener.requests[1].full_url.endswith("/contents/generations/tasks/job-42"))

    def test_ark_reference_roles_do_not_turn_into_endpoint_frames(self):
        adapter = self.adapter("ark-video", "video", {"id": "job-42"})
        adapter.submit({"prompt": "move", "mode": "reference_to_video", "references": [{"path": "a.png", "role": "subject"}, {"path": "b.png", "role": "style"}]}, self.root)
        body = json.loads(adapter.opener.requests[0].data)
        self.assertEqual([i["role"] for i in body["content"][1:]], ["reference_image", "reference_image"])
        self.assertIn("(style)", body["content"][0]["text"])

    def test_ark_rejects_multiple_candidates_in_one_task(self):
        adapter = self.adapter("ark-video", "video")
        self.assert_code("unsupported_parameter", lambda: adapter.submit({"prompt": "test", "output": {"count": 2}}, self.root))

    def test_ark_preserves_valid_task_id_when_initial_status_is_malformed(self):
        adapter = self.adapter("ark-video", "video", {"id": "accepted-task", "status": {"unexpected": True}})
        result = adapter.submit({"prompt": "test"}, self.root)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["job"]["remote_id"], "accepted-task")

    def test_failed_ark_poll_is_not_success(self):
        adapter = self.adapter("ark-video", "video", {"id": "job-42"}, {"id": "job-42", "status": "failed", "error": {"message": "failure"}})
        submitted = adapter.submit({"prompt": "test"}, self.root)
        self.assertEqual(adapter.poll(submitted["job"])["status"], "failed")

    def test_gemini_first_last_payload_and_operation_lifecycle(self):
        operation = "models/opaque-company-model/operations/job-9"
        adapter = self.adapter("gemini-veo", "video", {"name": operation}, {"name": operation, "done": True, "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": "https://media.example/movie.mp4"}}]}}})
        submitted = adapter.submit({"prompt": "move", "mode": "first_last_to_video", "start_frame": {"path": "a.png"}, "end_frame": {"path": "b.png"}, "parameters": {"duration_seconds": 8, "aspect_ratio": "16:9"}}, self.root)
        sent = adapter.opener.requests[0]
        body = json.loads(sent.data)
        self.assertTrue(sent.full_url.endswith("/models/opaque-company-model:predictLongRunning"))
        self.assertIn("inlineData", body["instances"][0]["image"])
        self.assertIn("lastFrame", body["instances"][0])
        self.assertNotIn("lastFrame", body.get("parameters", {}))
        self.assertEqual(body["parameters"], {"durationSeconds": 8, "aspectRatio": "16:9"})
        done = adapter.poll(submitted["job"])
        self.assertEqual(done["outputs"][0]["kind"], "video")
        self.assertEqual(adapter.opener.requests[1].full_url, "https://company.example/prefix/" + operation)

    def test_gemini_multiple_reference_order_and_no_implicit_endpoint_frames(self):
        adapter = self.adapter("gemini-veo", "video", {"name": "operations/test"})
        adapter.submit({"prompt": "move", "mode": "reference_to_video", "references": [{"path": "b.png", "position": 1}, {"path": "a.png", "position": 2}]}, self.root)
        instance = json.loads(adapter.opener.requests[0].data)["instances"][0]
        self.assertEqual([r["referenceType"] for r in instance["referenceImages"]], ["asset", "asset"])
        self.assertEqual(base64.b64decode(instance["referenceImages"][0]["image"]["inlineData"]["data"]), PNG + b"second fixture")
        self.assertNotIn("image", instance)
        self.assertNotIn("lastFrame", instance)

    def test_no_mixed_references_and_frames_in_known_protocols(self):
        adapter = self.adapter("gemini-veo", "video")
        self.assert_code("mode_inputs", lambda: adapter.submit({"prompt": "move", "start_frame": {"path": "a.png"}, "references": [{"path": "b.png"}]}, self.root))

    def test_resume_rejects_changed_model_route_or_connection(self):
        adapter = self.adapter("gemini-veo", "video", {"name": "operations/test"})
        job = adapter.submit({"prompt": "move"}, self.root)["job"]
        adapter.route["model"] = "a-different-model"
        self.assert_code("job_route_mismatch", lambda: adapter.poll(job))
        self.assertEqual(len(adapter.opener.requests), 1)

    def test_json_error_after_submit_is_unknown_no_auto_retry(self):
        adapter = self.adapter("ark-video", "video", b"gateway accepted but returned HTML")
        self.assert_code("submission_unknown", lambda: adapter.submit({"prompt": "move"}, self.root))
        self.assertEqual(len(adapter.opener.requests), 1)

    def test_empty_or_html_media_is_not_saved_as_video(self):
        adapter = self.adapter("ark-video", "video", b"<html>sign in</html>")
        self.assert_code("media_format", lambda: adapter.fetch({"status": "succeeded", "outputs": [{"kind": "video", "url": "https://store.example/f.mp4"}]}, self.root / "out"))
        self.assertEqual(list((self.root / "out").iterdir()), [])

    def test_download_retry_uses_saved_result_and_does_not_resubmit(self):
        adapter = self.adapter("ark-video", "video", URLError("temporary download error"), VIDEO_CONTAINER_FIXTURE)
        result = {"status": "succeeded", "outputs": [{"kind": "video", "url": "https://store.example/f.mp4"}]}
        self.assert_code("network_error", lambda: adapter.fetch(result, self.root / "out"))
        files = adapter.fetch(result, self.root / "out")
        self.assertEqual(len(files), 1)
        self.assertEqual([r.get_method() for r in adapter.opener.requests], ["GET", "GET"])


if __name__ == "__main__":
    unittest.main()
