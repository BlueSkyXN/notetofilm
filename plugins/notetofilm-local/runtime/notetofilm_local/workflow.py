"""Concrete dependency plans; creative decisions remain in the Skills."""

from __future__ import annotations

import copy
from pathlib import Path

from .errors import NTFError
from .io import atomic_json, digest, file_digest, now, project_lock
from .project import Project, check_id

KINDS = {"image", "video", "audio", "compose", "manual"}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version", 1) != 1 or not isinstance(plan.get("actions"), list):
        raise NTFError("plan_format", "Plan needs schema_version 1 and an actions array.")
    actions = {}
    for action in plan["actions"]:
        if not isinstance(action, dict):
            raise NTFError("plan_format", "Each action must be an object.")
        aid = check_id(action.get("id"), "action ID")
        if aid in actions:
            raise NTFError("duplicate_action", f"Duplicate action: {aid}")
        if action.get("kind") not in KINDS:
            raise NTFError("action_kind", f"Unsupported action kind for {aid}.")
        if not isinstance(action.get("request", {}), dict):
            raise NTFError("plan_format", f"{aid}.request must be an object.")
        if not isinstance(action.get("needs", []), list) or any(not isinstance(n, str) for n in action.get("needs", [])):
            raise NTFError("plan_format", f"{aid}.needs must be an array of action IDs.")
        if action.get("selection_slot"):
            check_id(action["selection_slot"], "selection slot")
        actions[aid] = action
    visiting, done = set(), set()

    def visit(aid):
        if aid in visiting:
            raise NTFError("dependency_cycle", f"Dependency cycle includes {aid}.")
        if aid in done:
            return
        if aid not in actions:
            raise NTFError("unknown_dependency", f"Dependency is not an action in this plan: {aid}")
        visiting.add(aid)
        for parent in actions[aid].get("needs", []):
            visit(parent)
        visiting.remove(aid)
        done.add(aid)

    for aid in actions:
        visit(aid)
    return actions


def save_plan(project: Project, plan):
    validate_plan(plan)
    plan = copy.deepcopy(plan)
    plan["schema_version"] = 1
    plan.setdefault("goal", "compose")
    with project_lock(project.root):
        atomic_json(project.root / "workflow.json", plan)
        state = project.state
        for action in plan["actions"]:
            state["actions"].setdefault(action["id"], {"status": "pending", "attempts": [], "outputs": []})
        atomic_json(project.root / "state.json", state)
    return refresh(project)


def field(scene_id, name, default=None):
    return {"storyboard_ref": {"scene_id": scene_id, "field": name, "default": default}}


def make_plan(project: Project):
    scenes = project.storyboard.get("scenes", [])
    if not scenes:
        raise NTFError("missing_storyboard", "Import a storyboard or provide an explicit plan for a single-media task.")
    actions, compose_scenes, all_dependencies = [], [], []
    defaults = project.document.get("defaults", {})
    for scene in scenes:
        sid = scene["id"]
        audio_policy = scene.get("audio_policy", "edge_tts")
        compose_scene = {"id": sid, "audio_policy": audio_policy}
        for key in ("duration_seconds", "native_volume", "narration_volume", "subtitles_offset_seconds"):
            if key in scene:
                compose_scene[key] = field(sid, key)
        if scene.get("audio"):
            compose_scene["audio"] = field(sid, "audio")
        elif audio_policy in ("edge_tts", "mix"):
            if not scene.get("narration", "").strip():
                raise NTFError("missing_narration", f"{sid} needs narration, an existing audio input, or a silent/native sound policy.")
            audio_id = f"{sid}.audio"
            request = {"text": field(sid, "narration", ""),
                       "voice": field(sid, "voice", defaults.get("voice", "zh-CN-XiaoxiaoNeural")),
                       "rate": field(sid, "rate", defaults.get("rate", "+0%")),
                       "volume": field(sid, "volume", "+0%"), "pitch": field(sid, "pitch", "+0Hz")}
            if "speech_text" in scene:
                request["speech_text"] = field(sid, "speech_text")
            actions.append({"id": audio_id, "scene_id": sid, "kind": "audio", "needs": [],
                            "request": request, "selection_slot": audio_id})
            compose_scene["audio"] = {"selection": audio_id}
            all_dependencies.append(audio_id)
        if scene.get("subtitles_path"):
            compose_scene["subtitles_path"] = field(sid, "subtitles_path")
        if scene.get("visuals"):
            compose_scene["visuals"] = field(sid, "visuals")
            compose_scenes.append(compose_scene)
            continue

        visual_type = scene.get("visual_type", "still")
        if visual_type not in ("still", "video"):
            raise NTFError("visual_type", f"{sid}.visual_type must be still or video.")
        video_mode = scene.get("video_mode", "image_to_video")
        needs_image = visual_type == "still" or (video_mode == "image_to_video" and not scene.get("start_frame"))
        image_id = f"{sid}.image"
        if needs_image:
            if not scene.get("visual_prompt", "").strip():
                raise NTFError("missing_visual_prompt", f"{sid} needs a visual_prompt or existing visuals/start_frame.")
            refs = scene.get("references", [])
            image_request = {"mode": "reference_to_image" if refs else "text_to_image",
                             "prompt": field(sid, "visual_prompt", ""), "references": field(sid, "references", []),
                             "output": field(sid, "image_output", {"count": 1})}
            if scene.get("image_route") or defaults.get("image_route"):
                image_request["route"] = field(sid, "image_route", defaults.get("image_route"))
            actions.append({"id": image_id, "kind": "image", "scene_id": sid, "needs": [],
                            "request": image_request, "selection_slot": image_id})
        if visual_type == "still":
            compose_scene["visuals"] = [{"selection": image_id}]
            all_dependencies.append(image_id)
        else:
            if video_mode not in ("text_to_video", "image_to_video", "reference_to_video", "first_last_to_video"):
                raise NTFError("video_mode", f"Unknown video mode for {sid}.")
            video_id = f"{sid}.video"
            prompt_field = "video_prompt" if scene.get("video_prompt") else "visual_prompt"
            if not scene.get(prompt_field, "").strip():
                raise NTFError("missing_video_prompt", f"{sid} needs an action/camera prompt.")
            request = {"mode": video_mode, "prompt": field(sid, prompt_field, ""),
                       "parameters": field(sid, "video_parameters", {})}
            if scene.get("route") or defaults.get("video_route"):
                request["route"] = field(sid, "route", defaults.get("video_route"))
            if video_mode in ("image_to_video", "first_last_to_video"):
                if needs_image:
                    request["start_frame"] = {"selection": image_id}
                elif scene.get("start_frame"):
                    request["start_frame"] = field(sid, "start_frame")
                else:
                    raise NTFError("missing_frame", f"{sid} requires start_frame.")
            if video_mode == "first_last_to_video":
                if not scene.get("end_frame"):
                    raise NTFError("missing_frame", f"{sid} requires end_frame.")
                request["end_frame"] = field(sid, "end_frame")
            if video_mode == "reference_to_video":
                request["references"] = field(sid, "video_references", scene.get("references", []))
            actions.append({"id": video_id, "kind": "video", "scene_id": sid,
                            "needs": [image_id] if needs_image else [], "request": request, "selection_slot": video_id})
            visual = {"selection": video_id}
            if "hold" in scene:
                visual["hold"] = field(sid, "hold", False)
            compose_scene["visuals"] = [visual]
            all_dependencies.append(video_id)
        compose_scenes.append(compose_scene)
    actions.append({"id": "film.compose", "kind": "compose", "needs": list(dict.fromkeys(all_dependencies)),
                    "request": {"manifest": {"output": project.document.get("output", {}), "scenes": compose_scenes}}})
    return {"schema_version": 1, "goal": "compose", "actions": actions}


def action_request(project, action, state=None):
    request = project.resolve(action.get("request", {}), state)
    if action["kind"] in ("image", "video", "audio"):
        request["kind"] = action["kind"]
    request["action_id"] = action["id"]
    if action.get("scene_id"):
        request["scene_id"] = action["scene_id"]
    return request


def refresh(project: Project):
    plan = project.plan
    validate_plan(plan)
    with project.transaction() as state:
        for action in plan["actions"]:
            record = state["actions"].setdefault(action["id"], {"status": "pending", "attempts": [], "outputs": []})
            record.pop("blocked_reason", None)
            try:
                request = action_request(project, action, state)
                current = digest(request)
                record["current_fingerprint"] = current
                if record.get("status") == "complete" and record.get("fingerprint") != current:
                    record["status"] = "inputs_changed"
                if record.get("status") == "inputs_changed" and record.get("fingerprint") == current:
                    record["status"] = "complete"
                if record.get("status") == "complete":
                    for aid in record.get("outputs", []):
                        asset = state["assets"].get(aid)
                        if asset is None or not project.path(asset["path"]).is_file():
                            record["status"] = "missing_result"
                            break
                        if file_digest(project.path(asset["path"])) != asset["sha256"]:
                            record["status"] = "missing_result"
                            break
                    if action["kind"] == "compose" and record.get("render"):
                        render = state["renders"].get(record["render"], {})
                        if not render.get("video_path") or not project.path(render["video_path"]).is_file():
                            record["status"] = "missing_result"
            except NTFError as exc:
                record["blocked_reason"] = exc.as_dict()
                if record.get("status") == "complete":
                    record["status"] = "inputs_changed"
        # A previously completed dependent cannot count as current while an input producer is stale.
        changed = True
        while changed:
            changed = False
            for action in plan["actions"]:
                record = state["actions"][action["id"]]
                if record.get("status") == "complete" and any(state["actions"].get(n, {}).get("status") != "complete" for n in action.get("needs", [])):
                    record["status"] = "inputs_changed"
                    changed = True
    return status(project, refresh_first=False)


def status(project, refresh_first=True):
    if refresh_first:
        return refresh(project)
    state, plan = project.state, project.plan
    rows = []
    for action in plan["actions"]:
        record = state["actions"].get(action["id"], {})
        dependencies = [n for n in action.get("needs", []) if state["actions"].get(n, {}).get("status") != "complete"]
        ready = record.get("status", "pending") in ("pending", "inputs_changed", "missing_result") and not dependencies and not record.get("blocked_reason") and action["kind"] != "manual"
        row = {"id": action["id"], "kind": action["kind"], "status": record.get("status", "pending"),
               "ready": ready, "waiting_for": dependencies, "outputs": record.get("outputs", [])}
        if record.get("blocked_reason"):
            row["blocked_reason"] = record["blocked_reason"]
        if record.get("error"):
            row["error"] = record["error"]
        if action.get("selection_slot") and record.get("status") == "complete" and action["selection_slot"] not in state["selection"]:
            row["needs_selection"] = action["selection_slot"]
        rows.append(row)
    standalone_complete = not rows and any(r.get("video_path") and project.path(r["video_path"]).is_file()
                                          for r in state["renders"].values())
    return {"project": str(project.root), "goal": plan.get("goal"), "actions": rows,
            "complete": standalone_complete or (bool(rows) and all(r["status"] == "complete" and not r.get("needs_selection") for r in rows)),
            "asset_count": len(state["assets"]), "selection": state["selection"], "renders": state["renders"]}
