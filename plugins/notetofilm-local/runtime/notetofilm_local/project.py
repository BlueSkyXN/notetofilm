"""Persistent local project, immutable media assets and input references."""

from __future__ import annotations

import copy
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import NTFError
from .io import atomic_json, file_digest, inside, now, project_lock, read_json

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def check_id(value, what="ID"):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise NTFError("invalid_id", f"Invalid {what}: use letters, digits, dots, underscores or hyphens.")
    return value


def init_project(root, title="Untitled film"):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with project_lock(root):
        if (root / "project.json").exists():
            raise NTFError("project_exists", "Project already exists; open it instead of initializing again.")
        for directory in ("sources", "assets/images", "assets/videos", "assets/audio", "assets/layouts", "renders", "work", "views"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        atomic_json(root / "project.json", {
            "schema_version": 1, "project_id": "film_" + uuid.uuid4().hex[:12],
            "title": title, "created_at": now(), "brief": {},
            "defaults": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%"},
            "output": {"width": 1280, "height": 720, "fps": 30},
        })
        atomic_json(root / "workflow.json", {"schema_version": 1, "goal": "media", "actions": []})
        atomic_json(root / "state.json", {
            "schema_version": 1, "actions": {}, "attempts": {}, "assets": {}, "selection": {}, "renders": {},
        })
    return Project(root)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / "project.json").is_file():
            raise NTFError("not_a_project", "Project is missing project.json; run ntf init first.")
        if self.document.get("schema_version") != 1:
            raise NTFError("project_version", "Unsupported project schema version.")

    @property
    def document(self):
        return read_json(self.root / "project.json")

    @property
    def state(self):
        return read_json(self.root / "state.json")

    @property
    def plan(self):
        return read_json(self.root / "workflow.json")

    @property
    def storyboard(self):
        path = self.root / "storyboard.json"
        return read_json(path) if path.is_file() else {"schema_version": 1, "scenes": []}

    @contextmanager
    def transaction(self):
        with project_lock(self.root):
            state = self.state
            yield state
            atomic_json(self.root / "state.json", state)

    def path(self, value):
        return inside(self.root, value)

    def put_storyboard(self, value):
        if isinstance(value, list):
            value = {"schema_version": 1, "scenes": value}
        if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
            raise NTFError("storyboard_format", "Storyboard must be an object with schema_version 1 and scenes.")
        scenes = value.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise NTFError("storyboard_format", "Storyboard requires a non-empty scenes array.")
        seen = set()
        for scene in scenes:
            if not isinstance(scene, dict):
                raise NTFError("storyboard_format", "Each scene must be an object.")
            sid = check_id(scene.get("id"), "scene ID")
            if sid in seen:
                raise NTFError("duplicate_scene", f"Duplicate scene ID: {sid}")
            seen.add(sid)
            for field in ("narration", "visual_prompt", "video_prompt"):
                if field in scene and not isinstance(scene[field], str):
                    raise NTFError("storyboard_format", f"{sid}.{field} must be text.")
            if scene.get("audio_policy", "edge_tts") not in ("edge_tts", "native", "mix", "silent"):
                raise NTFError("audio_policy", f"Unknown sound policy for {sid}.")
        value = copy.deepcopy(value)
        value["schema_version"] = 1
        with project_lock(self.root):
            atomic_json(self.root / "storyboard.json", value)
        self.write_storyboard_view(value)
        return {"scene_count": len(scenes), "path": "storyboard.json"}

    def write_storyboard_view(self, storyboard=None):
        storyboard = storyboard or self.storyboard
        parts = [f"# {self.document.get('title', 'Film')}\n"]
        for scene in storyboard["scenes"]:
            parts.extend([f"## {scene['id']}\n", scene.get("narration", ""),
                          "\n画面：" + scene.get("visual_prompt", ""),
                          "\n动作：" + scene.get("video_prompt", "")])
        (self.root / "views").mkdir(parents=True, exist_ok=True)
        (self.root / "views" / "storyboard.md").write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    def import_asset(self, source, kind=None, selection_slot=None):
        source = Path(source).resolve()
        if not source.is_file():
            raise NTFError("missing_file", "The media file does not exist.", {"path": str(source)})
        from .media import probe
        metadata = probe(source)
        actual_kind = metadata.get("kind")
        if kind is not None and actual_kind != kind:
            raise NTFError("media_kind", f"Requested {kind} but the file is {actual_kind}.")
        kind = kind or actual_kind
        if kind not in ("image", "video", "audio"):
            raise NTFError("media_kind", "Only image, video and audio files can be registered as media.")
        aid = "asset_" + uuid.uuid4().hex[:16]
        directory = {"image": "images", "video": "videos", "audio": "audio"}[kind]
        destination = self.root / "assets" / directory / (aid + source.suffix.lower())
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return self.register_file(destination, kind, {"type": "import", "original_name": source.name}, selection_slot, aid)

    def register_file(self, path, kind=None, source=None, selection_slot=None, asset_id=None, metadata=None):
        path = self.path(path)
        if not path.is_file():
            raise NTFError("missing_file", "Cannot register a missing media file.", {"path": str(path)})
        from .media import probe
        metadata = copy.deepcopy(metadata) if metadata is not None else probe(path)
        metadata.pop("path", None)
        actual_kind = metadata.get("kind")
        if kind is not None and actual_kind != kind:
            raise NTFError("media_kind", f"Expected {kind}; received {actual_kind}.")
        kind = kind or actual_kind
        aid = asset_id or "asset_" + uuid.uuid4().hex[:16]
        record = {"id": aid, "path": path.relative_to(self.root).as_posix(), "kind": kind,
                  "metadata": metadata, "sha256": file_digest(path), "created_at": now(), "source": source or {}}
        with self.transaction() as state:
            if aid in state["assets"]:
                raise NTFError("asset_exists", "An asset ID cannot be overwritten.")
            state["assets"][aid] = record
            if selection_slot:
                check_id(selection_slot, "selection slot")
                state["selection"][selection_slot] = aid
        return record

    def select(self, slot, asset_ids):
        check_id(slot, "selection slot")
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        if not asset_ids:
            raise NTFError("empty_selection", "Select at least one asset.")
        with self.transaction() as state:
            for aid in asset_ids:
                if aid not in state["assets"]:
                    raise NTFError("unknown_asset", f"Unknown asset: {aid}")
                if not self.path(state["assets"][aid]["path"]).is_file():
                    raise NTFError("missing_asset", f"Asset file is missing: {aid}")
            state["selection"][slot] = asset_ids[0] if len(asset_ids) == 1 else list(asset_ids)
        return {"slot": slot, "assets": asset_ids}

    def resolve(self, value, state=None):
        state = state if state is not None else self.state
        scenes = {s["id"]: s for s in self.storyboard.get("scenes", [])}

        def asset(aid):
            record = state["assets"].get(aid)
            if record is None:
                raise NTFError("missing_input", f"Unknown input asset: {aid}")
            path = self.path(record["path"])
            if not path.is_file():
                raise NTFError("missing_input", f"Input asset file is missing: {aid}")
            if file_digest(path) != record["sha256"]:
                raise NTFError("asset_modified", f"Registered asset changed on disk: {aid}. Import the new version.")
            return {**record.get("metadata", {}), "asset_id": aid, "path": record["path"],
                    "kind": record["kind"], "sha256": record["sha256"]}

        def reference(aid, extra):
            resolved = asset(aid)
            for key in ("path", "kind", "sha256", "asset_id"):
                if key in extra and extra[key] != resolved[key]:
                    raise NTFError("asset_reference", f"A media reference cannot override its registered {key}.")
            return {**resolved, **extra}

        def visit(item):
            if isinstance(item, list):
                result = []
                for child in item:
                    value = visit(child)
                    if isinstance(child, dict) and "selection" in child and isinstance(value, list):
                        result.extend(value)
                    else:
                        result.append(value)
                return result
            if not isinstance(item, dict):
                return item
            if "storyboard_ref" in item:
                ref = item["storyboard_ref"]
                scene = scenes.get(ref.get("scene_id"))
                if scene is None:
                    raise NTFError("missing_input", "Referenced storyboard scene is missing.", ref)
                if ref["field"] not in scene and "default" not in ref:
                    raise NTFError("missing_input", "Referenced storyboard field is missing.", ref)
                return visit(scene.get(ref["field"], ref.get("default")))
            if "selection" in item:
                selected = state["selection"].get(item["selection"])
                if selected is None:
                    raise NTFError("missing_input", f"Select media for {item['selection']} first.", {"selection": item["selection"]})
                extra = {k: visit(v) for k, v in item.items() if k != "selection"}
                if isinstance(selected, list):
                    return [reference(aid, extra) for aid in selected]
                return reference(selected, extra)
            if "asset_id" in item:
                return reference(item["asset_id"], {k: visit(v) for k, v in item.items() if k != "asset_id"})
            result = {k: visit(v) for k, v in item.items()}
            if "path" in result:
                path = self.path(result["path"])
                if not path.is_file():
                    raise NTFError("missing_input", "Input file is missing.", {"path": result["path"]})
                result["path"] = path.relative_to(self.root).as_posix()
                result.setdefault("sha256", file_digest(path))
            return result

        return visit(value)
