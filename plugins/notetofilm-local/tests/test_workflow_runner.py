from __future__ import annotations

import copy
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from notetofilm_local.cli import add_request, main
from notetofilm_local.errors import NTFError
from notetofilm_local.io import digest
from notetofilm_local.project import init_project
from notetofilm_local.runner import _claim, execute_action, run
from notetofilm_local.workflow import action_request, make_plan, save_plan, status, validate_plan


class FakeAdapter:
    def __init__(self, mode="sync", count=1):
        self.mode, self.count = mode, count
        self.posts = self.polls = self.downloads = 0
        self.download_failures = 0
        self.routes = []

    def submit(self, request, project_root):
        self.posts += 1
        if self.mode == "unknown":
            raise NTFError("submission_unknown", "Connection ended after the request may have been accepted.")
        if self.mode == "async":
            return {"status": "submitted", "job": {"task_id": "test-job", "model": "opaque-test-model"}}
        return {"status": "succeeded", "items": []}

    def poll(self, job):
        self.polls += 1
        return {"status": "succeeded", "job": job, "items": []}

    def fetch(self, result, output_dir):
        self.downloads += 1
        if self.download_failures:
            self.download_failures -= 1
            raise NTFError("download_failed", "Temporary fixture download failure.")
        result = []
        for i in range(self.count):
            path = Path(output_dir) / f"candidate-{i}.png"
            path.write_bytes(f"fixture-{i}".encode())
            result.append({"path": str(path), "kind": "image"})
        return result


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = init_project(Path(self.temp.name) / "film", "Local test")
        self.probe = patch("notetofilm_local.media.probe", side_effect=lambda path: {
            "path": str(Path(path).resolve()),
            "kind": "audio" if str(path).endswith(".wav") else "video" if str(path).endswith(".mp4") else "image",
            "duration_seconds": 2.0, "width": 640, "height": 360,
        })
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def asset(self, name):
        path = self.project.root / "assets" / name
        path.write_bytes(name.encode())
        return self.project.register_file(path)

    def plan(self, kind="image", slot="s01.image"):
        value = {"actions": [{"id": "generate", "kind": kind, "needs": [],
                              "selection_slot": slot, "request": {"mode": "text_to_image", "prompt": "A test image"}}]}
        save_plan(self.project, value)
        return self.project.plan["actions"][0]

    def adapter_context(self, adapter):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("notetofilm_local.config.resolve_route", side_effect=lambda config, route, kind: {
            "id": "configured-company", "model": "opaque-test-model", "adapter": "fixture",
            "connection": {"base_url": (config or {}).get("base", "https://first.invalid")},
        }))
        stack.enter_context(patch("notetofilm_local.capabilities.check_request"))
        def selected(route):
            adapter.routes.append(copy.deepcopy(route))
            return adapter
        stack.enter_context(patch("notetofilm_local.adapters.get_adapter", side_effect=selected))
        self.addCleanup(stack.close)
        return stack

    def test_cycles_and_missing_dependencies_are_rejected(self):
        for needs, code in ((["a"], "dependency_cycle"), (["missing"], "unknown_dependency")):
            with self.assertRaises(NTFError) as error:
                validate_plan({"actions": [{"id": "a", "kind": "image", "needs": needs}]})
            self.assertEqual(error.exception.code, code)

    def test_multireference_order_and_external_path_rejection(self):
        first, second = self.asset("first.png"), self.asset("second.png")
        self.project.select("cast", [second["id"], first["id"]])
        result = self.project.resolve({"references": [{"selection": "cast", "role": "character"}]})
        self.assertEqual([r["asset_id"] for r in result["references"]], [second["id"], first["id"]])
        self.assertEqual(result["references"][0]["role"], "character")
        with self.assertRaises(NTFError) as error:
            self.project.resolve({"path": "../outside.png"})
        self.assertEqual(error.exception.code, "outside_project")

    def test_registered_reference_stays_project_relative_and_cannot_override_identity(self):
        asset = self.asset("portable.png")
        resolved = self.project.resolve({"asset_id": asset["id"]})
        self.assertEqual(resolved["path"], "assets/portable.png")
        self.assertNotIn("path", asset["metadata"])
        with self.assertRaises(NTFError):
            self.project.resolve({"asset_id": asset["id"], "path": "../other.png"})

    def test_standalone_render_is_complete_without_a_generation_plan(self):
        path = self.project.root / "renders" / "sample.mp4"
        path.write_bytes(b"fixture")
        with self.project.transaction() as state:
            state["renders"]["sample"] = {"video_path": "renders/sample.mp4"}
        self.assertTrue(status(self.project)["complete"])

    def test_project_restored_without_empty_directories_can_import_and_edit(self):
        # Source archives do not retain empty assets/images or views directories.
        (self.project.root / "assets/images").rmdir()
        (self.project.root / "views").rmdir()
        external = Path(self.temp.name) / "source.png"
        external.write_bytes(b"fixture")
        imported = self.project.import_asset(external)
        self.assertTrue(self.project.path(imported["path"]).is_file())
        self.project.put_storyboard({"scenes": [{"id": "s1", "narration": "Restored project"}]})
        self.assertTrue((self.project.root / "views/storyboard.md").is_file())

    def test_editing_narration_invalidates_audio_and_compose_only(self):
        board = {"scenes": [{"id": "s01", "narration": "Original speech", "visual_type": "video",
                             "video_mode": "text_to_video", "video_prompt": "Slow camera move"}]}
        self.project.put_storyboard(board)
        save_plan(self.project, make_plan(self.project))
        for kind, suffix in (("audio", "wav"), ("video", "mp4")):
            asset = self.asset("existing." + suffix)
            self.project.select("s01." + kind, asset["id"])
        with self.project.transaction() as state:
            for action in self.project.plan["actions"]:
                state["actions"][action["id"]].update({"status": "complete", "fingerprint": digest(action_request(self.project, action, state))})
        self.assertTrue(status(self.project)["complete"])
        board["scenes"][0]["narration"] = "Changed speech"
        self.project.put_storyboard(board)
        current = {r["id"]: r for r in status(self.project)["actions"]}
        self.assertEqual(current["s01.video"]["status"], "complete")
        self.assertEqual(current["s01.audio"]["status"], "inputs_changed")
        self.assertEqual(current["film.compose"]["status"], "inputs_changed")
        video = next(a for a in self.project.plan["actions"] if a["id"] == "s01.video")
        self.assertNotIn("s01.audio", video["needs"])

    def test_sync_completion_is_not_regenerated_on_resume(self):
        self.plan()
        adapter = FakeAdapter()
        with self.adapter_context(adapter):
            self.assertTrue(run(self.project)["complete"])
            self.assertTrue(run(self.project)["complete"])
        self.assertEqual(adapter.posts, 1)
        self.assertEqual(adapter.downloads, 1)

    def test_async_resume_uses_persisted_route_and_never_posts_twice(self):
        self.plan()
        adapter = FakeAdapter("async")
        with self.adapter_context(adapter):
            initial = run(self.project, {"base": "https://first.invalid"})
            self.assertEqual(initial["actions"][0]["status"], "submitted")
            self.assertTrue(run(self.project, {"base": "https://changed.invalid"})["complete"])
        self.assertEqual(adapter.posts, 1)
        self.assertEqual(adapter.polls, 1)
        self.assertEqual(adapter.routes[-1]["connection"]["base_url"], "https://first.invalid")

    def test_failed_download_can_resume_without_regeneration(self):
        self.plan()
        adapter = FakeAdapter()
        adapter.download_failures = 1
        with self.adapter_context(adapter):
            result = run(self.project)
            self.assertEqual(result["actions"][0]["status"], "waiting_download")
            self.assertTrue(run(self.project)["complete"])
        self.assertEqual((adapter.posts, adapter.downloads), (1, 2))

    def test_missing_download_refetches_existing_response(self):
        self.plan()
        adapter = FakeAdapter()
        with self.adapter_context(adapter):
            run(self.project)
            asset = next(iter(self.project.state["assets"].values()))
            self.project.path(asset["path"]).unlink()
            self.assertTrue(run(self.project)["complete"])
        self.assertEqual((adapter.posts, adapter.downloads), (1, 2))

    def test_unknown_submission_needs_deliberate_retry(self):
        self.plan()
        adapter = FakeAdapter("unknown")
        with self.adapter_context(adapter):
            self.assertEqual(run(self.project)["actions"][0]["status"], "unknown")
            run(self.project)
            self.assertEqual(adapter.posts, 1)
            run(self.project, action_id="generate", retry=True)
        self.assertEqual(adapter.posts, 2)
        self.assertEqual(len(self.project.state["attempts"]), 2)

    def test_old_selection_is_cleared_when_regeneration_has_multiple_candidates(self):
        self.plan()
        adapter = FakeAdapter()
        with self.adapter_context(adapter):
            run(self.project)
            self.assertIn("s01.image", self.project.state["selection"])
            adapter.count = 2
            result = run(self.project, action_id="generate", force=True)
        self.assertNotIn("s01.image", self.project.state["selection"])
        self.assertEqual(result["actions"][0]["needs_selection"], "s01.image")
        self.assertEqual(len(result["actions"][0]["outputs"]), 2)

    def test_live_owner_is_not_claimed_even_in_same_process(self):
        action = self.plan()
        request = action_request(self.project, action)
        first, _ = _claim(self.project, action, request)
        with self.assertRaises(NTFError) as error:
            _claim(self.project, action, request)
        self.assertEqual(error.exception.code, "action_busy")
        self.assertEqual(len(self.project.state["attempts"]), 1)

    def test_orphan_running_submission_is_saved_as_unknown(self):
        action = self.plan()
        request = action_request(self.project, action)
        first, _ = _claim(self.project, action, request)
        with self.project.transaction() as state:
            state["attempts"][first["id"]]["owner_pid"] = None
        with self.assertRaises(NTFError):
            _claim(self.project, action, request)
        self.assertEqual(self.project.state["actions"]["generate"]["status"], "unknown")

    def test_concurrent_asset_registration_keeps_all_records(self):
        paths = []
        for i in range(12):
            path = self.project.root / "assets" / f"parallel-{i}.png"
            path.write_bytes(str(i).encode())
            paths.append(path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            registered = list(pool.map(self.project.register_file, paths))
        self.assertEqual(len(self.project.state["assets"]), 12)
        self.assertEqual(len({r["id"] for r in registered}), 12)

    def test_standalone_request_appends_instead_of_replacing_plan(self):
        self.plan()
        aid = add_request(self.project, {"kind": "video", "mode": "text_to_video", "prompt": "A movement"})
        self.assertEqual(len(self.project.plan["actions"]), 2)
        self.assertNotEqual(aid, "generate")
        with self.assertRaises(NTFError):
            add_request(self.project, {"kind": "image", "action_id": "generate", "prompt": "duplicate"})


if __name__ == "__main__":
    unittest.main()
