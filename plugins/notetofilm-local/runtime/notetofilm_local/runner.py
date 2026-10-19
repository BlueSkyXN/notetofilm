"""Resumable execution of a finite local media plan."""

from __future__ import annotations

import copy
import os
import time
import uuid
from pathlib import Path

from .errors import NTFError
from .io import atomic_json, digest, now, process_alive, read_json
from .project import Project
from .workflow import action_request, refresh, status


def _attempt_path(project, attempt_id, name):
    return project.root / "work" / attempt_id / name


def _persist(project, action_id, attempt_id, **fields):
    with project.transaction() as state:
        attempt = state["attempts"][attempt_id]
        attempt.update(fields)
        attempt["updated_at"] = now()
        action = state["actions"][action_id]
        if action.get("current_attempt") == attempt_id:
            for key in ("status", "error", "outputs", "render"):
                if key in fields:
                    action[key] = fields[key]


def _claim(project, action, request, retry=False, force=False):
    fingerprint = digest(request)
    refusal = None
    with project.transaction() as state:
        record = state["actions"].setdefault(action["id"], {"status": "pending", "attempts": [], "outputs": []})
        current = state["attempts"].get(record.get("current_attempt"), {})
        if current.get("owner_pid") and process_alive(current["owner_pid"]):
            raise NTFError("action_busy", f"Another local process is handling {action['id']}.")
        same_input = current.get("fingerprint") == fingerprint
        pending_remote = bool(current.get("job")) and current.get("status") not in ("failed", "complete")
        if pending_remote and not same_input and not (force or retry):
            raise NTFError("pending_old_input", "An earlier request is still pending with different inputs. Resume it or explicitly retry this action.")
        if not force and same_input and current:
            can_resume = current.get("job") and current.get("status") not in ("failed", "complete")
            can_fetch = current.get("response_path") and (record.get("status") == "missing_result" or current.get("status") == "waiting_download")
            if can_resume or can_fetch:
                current["owner_pid"] = os.getpid()
                record["status"] = "running"
                return copy.deepcopy(current), True
            if current.get("status") == "running":
                interrupted_status = "unknown" if action["kind"] in ("image", "video") else "interrupted"
                current["status"] = interrupted_status
                current["owner_pid"] = None
                record["status"] = interrupted_status
            if record.get("status") in ("failed", "unknown", "interrupted") and not retry:
                refusal = NTFError("retry_required", f"{action['id']} requires --retry to create a new attempt.")
        if record.get("status") == "complete" and not force:
            raise NTFError("already_complete", f"{action['id']} is already complete.")
        if refusal is None:
            attempt_id = "attempt_" + uuid.uuid4().hex[:16]
            attempt = {"id": attempt_id, "action_id": action["id"], "kind": action["kind"],
                       "status": "running", "fingerprint": fingerprint, "started_at": now(),
                       "owner_pid": os.getpid(), "outputs": []}
            atomic_json(_attempt_path(project, attempt_id, "request.json"), request)
            state["attempts"][attempt_id] = attempt
            record.update({"status": "running", "current_attempt": attempt_id, "error": None})
            record.setdefault("attempts", []).append(attempt_id)
    if refusal is not None:
        raise refusal
    return attempt, False


def _register_outputs(project, action, attempt, files, audio_info=None):
    output_ids, primary_ids = [], []
    for output in files:
        if output.get("kind") not in ("image", "video", "audio"):
            continue
        path = project.path(output["path"])
        relative = path.relative_to(project.root).as_posix()
        existing = next((a for a in project.state["assets"].values()
                         if a["path"] == relative and a.get("source", {}).get("attempt_id") == attempt["id"]), None)
        if existing:
            from .io import file_digest
            if path.is_file() and file_digest(path) == existing["sha256"]:
                output_ids.append(existing["id"])
                if output["kind"] == action["kind"]:
                    primary_ids.append(existing["id"])
                continue
        from .media import probe
        metadata = probe(path)
        if output.get("role"):
            metadata["role"] = output["role"]
        if audio_info and output["kind"] == "audio":
            for key in ("boundaries_path", "subtitles_path"):
                if audio_info.get(key):
                    metadata[key] = project.path(audio_info[key]).relative_to(project.root).as_posix()
            metadata["subtitle_status"] = audio_info.get("subtitle_status", "available" if audio_info.get("subtitles_path") else "unavailable")
        asset = project.register_file(path, output["kind"], {
            "type": "generation", "action_id": action["id"], "attempt_id": attempt["id"],
            "fingerprint": attempt["fingerprint"], "request_path": f"work/{attempt['id']}/request.json",
        }, metadata=metadata)
        output_ids.append(asset["id"])
        if output["kind"] == action["kind"]:
            primary_ids.append(asset["id"])
    if not primary_ids:
        raise NTFError("no_media", "The operation did not produce any actual image, video or audio file.")
    index = action.get("select_index")
    if index is not None and (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(primary_ids)):
        raise NTFError("selection_index", "select_index is outside the returned primary candidate list.")
    with project.transaction() as state:
        record = state["actions"][action["id"]]
        state["attempts"][attempt["id"]].update({"status": "complete", "outputs": output_ids, "owner_pid": None, "finished_at": now()})
        if record.get("current_attempt") == attempt["id"]:
            record.update({"status": "complete", "outputs": output_ids, "fingerprint": attempt["fingerprint"], "error": None})
            slot = action.get("selection_slot")
            if slot and index is not None:
                state["selection"][slot] = primary_ids[index]
            elif slot and len(primary_ids) == 1:
                state["selection"][slot] = primary_ids[0]
            elif slot:
                state["selection"].pop(slot, None)
    return output_ids


def _prepare_manifest(project, manifest):
    manifest = copy.deepcopy(manifest)
    for scene in manifest.get("scenes", []):
        audio = scene.get("audio")
        if isinstance(audio, dict) and audio.get("subtitles_path") and not scene.get("subtitles_path"):
            scene["subtitles_path"] = audio["subtitles_path"]
    return manifest


def compose_project(project, manifest, render=True, attempt_id=None):
    from .compose import compose
    rid = "r_" + uuid.uuid4().hex[:12]
    output_dir = project.root / "renders" / rid
    output_dir.mkdir(parents=True)
    resolved = project.resolve(manifest)
    resolved = _prepare_manifest(project, resolved)
    atomic_json(output_dir / "inputs.json", {"project": project.document, "storyboard": project.storyboard,
                                              "selection": project.state["selection"], "manifest": resolved})
    result = compose(project.root, resolved, output_dir, render=render)
    normalized = {}
    for key, value in result.items():
        if key.endswith("_path") and value:
            normalized[key] = project.path(value).relative_to(project.root).as_posix()
        else:
            normalized[key] = value
    normalized["id"] = rid
    normalized["created_at"] = now()
    if render and not normalized.get("video_path"):
        normalized["video_path"] = normalized.get("mp4_path")
    if render and (not normalized.get("video_path") or not project.path(normalized["video_path"]).is_file()):
        raise NTFError("render_missing", "Encoding finished without a complete video file.")
    with project.transaction() as state:
        state["renders"][rid] = normalized
    return normalized


def execute_action(project, action, config=None, retry=False, force=False):
    request = action_request(project, action)
    attempt, resumed = _claim(project, action, request, retry=retry, force=force)
    aid, tid = action["id"], attempt["id"]
    kind = action["kind"]
    output_directory = {"image": "images", "video": "videos", "audio": "audio"}.get(kind, "other")
    output_dir = project.root / "assets" / output_directory / tid
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "audio":
            from .adapters.edge_tts import synthesize
            options = {key: request[key] for key in ("voice", "rate", "volume", "pitch", "speech_text") if request.get(key) is not None}
            info = synthesize(request.get("text", request.get("narration", "")), output_dir, **options)
            files = info.get("files", [])
            _register_outputs(project, action, attempt, files, audio_info=info)
        elif kind == "compose":
            result = compose_project(project, request.get("manifest", {}), attempt_id=tid)
            _persist(project, aid, tid, status="complete", render=result["id"], owner_pid=None, finished_at=now())
            with project.transaction() as state:
                if state["actions"][aid].get("current_attempt") == tid:
                    state["actions"][aid]["fingerprint"] = attempt["fingerprint"]
        elif kind in ("image", "video"):
            from .adapters import get_adapter
            from .capabilities import check_request
            from .config import load_config, resolve_route
            route_path = _attempt_path(project, tid, "route.json")
            if resumed and route_path.is_file():
                route = read_json(route_path)
            else:
                route = resolve_route(config if config is not None else load_config(), request.get("route"), kind)
                check_request(route, request)
                atomic_json(route_path, route)
            adapter = get_adapter(route)
            response_path = _attempt_path(project, tid, "response.json")
            if resumed and attempt.get("response_path") and (attempt.get("status") in ("waiting_download", "complete") or project.state["actions"][aid].get("status") == "missing_result"):
                result = read_json(project.path(attempt["response_path"]))
            elif resumed and attempt.get("job"):
                result = adapter.poll(attempt["job"])
            else:
                # Route mismatch is reported before submit; no automatic POST retries.
                result = adapter.submit(request, project.root)
            if not isinstance(result, dict) or result.get("status") not in ("submitted", "succeeded", "failed"):
                raise NTFError("adapter_result", "Adapter returned an invalid normalized result.")
            if result.get("job"):
                _persist(project, aid, tid, job=result["job"], route_path=route_path.relative_to(project.root).as_posix())
            if result["status"] == "submitted":
                if not result.get("job"):
                    raise NTFError("submission_unknown", "Company API accepted work but returned no resumable task information.")
                _persist(project, aid, tid, status="submitted", owner_pid=None)
            elif result["status"] == "failed":
                raise NTFError("generation_failed", "The company generation task failed.", result.get("error") or {})
            else:
                atomic_json(response_path, result)
                _persist(project, aid, tid, status="waiting_download", response_path=response_path.relative_to(project.root).as_posix())
                files = adapter.fetch(result, output_dir)
                _register_outputs(project, action, attempt, files)
        else:
            raise NTFError("manual_action", "This action needs the Agent to produce its input before continuing.")
    except Exception as exc:
        error = exc.as_dict() if isinstance(exc, NTFError) else {"code": "local_operation_failed", "message": "Operation failed locally.", "details": {"exception": type(exc).__name__}}
        current = project.state["attempts"][tid]
        if current.get("status") == "waiting_download":
            outcome = "waiting_download"
        elif error["code"] == "submission_unknown":
            outcome = "unknown"
        elif current.get("job") and error["code"] not in ("generation_failed",):
            outcome = "submitted"
        else:
            outcome = "failed"
        _persist(project, aid, tid, status=outcome, error=error, owner_pid=None)
        return {"action": aid, "attempt": tid, "status": outcome, "error": error}
    except KeyboardInterrupt:
        current = project.state["attempts"][tid]
        outcome = "submitted" if current.get("job") else ("unknown" if kind in ("image", "video") else "interrupted")
        _persist(project, aid, tid, status=outcome, owner_pid=None)
        raise
    return {"action": aid, "attempt": tid, "status": project.state["attempts"][tid]["status"]}


def run(project, config=None, action_id=None, wait_seconds=0, retry=False, force=False):
    if wait_seconds < 0:
        raise NTFError("wait_seconds", "Wait budget must be nonnegative.")
    if force and not action_id:
        raise NTFError("force_scope", "Use --action with --force to identify the exact work to regenerate.")
    actions = {a["id"]: a for a in project.plan["actions"]}
    if action_id and action_id not in actions:
        raise NTFError("unknown_action", f"Unknown action: {action_id}")
    deadline = time.monotonic() + wait_seconds
    results, handled = [], set()
    while True:
        snapshot = refresh(project)
        candidates = []
        for row in snapshot["actions"]:
            if action_id and row["id"] != action_id:
                continue
            if row["waiting_for"] or row.get("blocked_reason"):
                continue
            record = project.state["actions"][row["id"]]
            attempt = project.state["attempts"].get(record.get("current_attempt"), {})
            running_orphan = row["status"] == "running" and not process_alive(attempt.get("owner_pid"))
            recoverable = row["status"] in ("submitted", "waiting_download") or running_orphan
            deliberate_retry = retry and row["status"] in ("failed", "unknown", "interrupted")
            deliberate_force = force and row["id"] == action_id
            if row["ready"] or recoverable or deliberate_retry or deliberate_force:
                if row["id"] not in handled or (recoverable and time.monotonic() < deadline):
                    candidates.append(row["id"])
        if not candidates:
            break
        for aid in candidates:
            try:
                result = execute_action(project, actions[aid], config, retry=retry and aid not in handled, force=force and aid not in handled)
            except NTFError as exc:
                result = {"action": aid, "status": "blocked", "error": exc.as_dict()}
            results.append(result)
            handled.add(aid)
        # Recompute ready dependencies immediately; only wait when all runnable work is remote.
        refreshed = refresh(project)
        newly_ready = any(r["ready"] and r["id"] not in handled and (not action_id or r["id"] == action_id) for r in refreshed["actions"])
        if newly_ready:
            continue
        if time.monotonic() >= deadline:
            break
        if not any(r["status"] in ("submitted", "waiting_download") for r in refreshed["actions"] if not action_id or r["id"] == action_id):
            break
        time.sleep(min(2.0, max(0, deadline - time.monotonic())))
    return {"executed": results, **status(project)}
