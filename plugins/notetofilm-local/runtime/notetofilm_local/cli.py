"""JSON command interface used by both people and the seven Skills."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import shutil
import sys
import uuid
from pathlib import Path

from . import __version__
from .errors import NTFError
from .io import atomic_json, read_json
from .project import Project, init_project


def parser():
    p = argparse.ArgumentParser(prog="ntf", description="NoteToFilm-local: local media projects and resumable workflows")
    p.add_argument("--version", action="version", version=f"NoteToFilm-local {__version__}")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a local project")
    init.add_argument("project")
    init.add_argument("--title", default="Untitled film")
    cfg = commands.add_parser("config-init", help="Write six unconfigured company model routes")
    cfg.add_argument("file")
    routes = commands.add_parser("routes", help="List configured model routes without calling them")
    routes.add_argument("--config")
    commands.add_parser("install-info", help="Show runtime and local media dependencies")
    imp = commands.add_parser("import", help="Copy actual media into the project and register it")
    imp.add_argument("project")
    imp.add_argument("file")
    imp.add_argument("--kind", choices=["image", "video", "audio"])
    imp.add_argument("--select")
    board = commands.add_parser("storyboard", help="Validate and save the formal storyboard")
    board.add_argument("project")
    board.add_argument("--file", required=True)
    plan = commands.add_parser("plan", help="Build a plan from the storyboard or load a custom DAG")
    plan.add_argument("project")
    plan.add_argument("--file")
    for name in ("ready", "status"):
        command = commands.add_parser(name, help="Inspect project state and current input dependencies")
        command.add_argument("project")
    run = commands.add_parser("run", help="Execute ready work or resume recorded tasks")
    run.add_argument("project")
    run.add_argument("--action")
    run.add_argument("--config")
    run.add_argument("--wait-seconds", type=float, default=0)
    run.add_argument("--retry", action="store_true", help="Allow a new attempt for failed/unknown/interrupted work")
    run.add_argument("--force", action="store_true", help="Regenerate the explicitly named action")
    select = commands.add_parser("select", help="Select one or ordered multiple assets for a slot")
    select.add_argument("project")
    select.add_argument("slot")
    select.add_argument("asset_ids", nargs="+")
    generate = commands.add_parser("generate", help="Add and run a standalone image or video request")
    generate.add_argument("project")
    generate.add_argument("--request", required=True)
    generate.add_argument("--config")
    generate.add_argument("--wait-seconds", type=float, default=0)
    audio = commands.add_parser("audio", help="Add and synthesize a standalone Edge TTS narration")
    audio.add_argument("project")
    audio.add_argument("--text", required=True)
    audio.add_argument("--speech-text")
    audio.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    audio.add_argument("--rate", default="+0%")
    audio.add_argument("--volume", default="+0%")
    audio.add_argument("--pitch", default="+0Hz")
    audio.add_argument("--action-id")
    audio.add_argument("--select")
    compose = commands.add_parser("compose", help="Compose an explicit mixed-media manifest")
    compose.add_argument("project")
    compose.add_argument("--manifest", required=True)
    compose.add_argument("--plan-only", action="store_true")
    return p


def add_request(project, payload):
    from .workflow import save_plan
    if not isinstance(payload, dict) or "request" in payload:
        raise NTFError("request_format", "Use a flat request object with kind, mode, prompt and optional references.")
    request = dict(payload)
    kind = request.pop("kind", None)
    if kind not in ("image", "video", "audio"):
        raise NTFError("request_kind", "Request kind must be image, video or audio.")
    aid = request.pop("action_id", kind + "." + uuid.uuid4().hex[:12])
    action = {"id": aid, "kind": kind, "needs": request.pop("needs", []), "request": request}
    for name in ("scene_id", "selection_slot", "select_index"):
        if name in request:
            action[name] = request.pop(name)
    plan = project.plan
    if any(a["id"] == aid for a in plan["actions"]):
        raise NTFError("action_exists", "Action already exists. Edit its plan and use ntf run --action; use --force for an intentional new version.")
    plan["actions"].append(action)
    save_plan(project, plan)
    return aid


def dispatch(args):
    from .config import default_config, list_routes, load_config
    from .runner import compose_project, run
    from .workflow import make_plan, save_plan, status
    if args.command == "init":
        project = init_project(args.project, args.title)
        return {"project": str(project.root), "project_id": project.document["project_id"]}
    if args.command == "config-init":
        path = Path(args.file).expanduser().resolve()
        if path.exists():
            raise NTFError("config_exists", "The configuration file already exists; edit it in place.")
        if path.suffix.lower() in (".yaml", ".yml", ".toml"):
            raise NTFError("config_format", "config-init writes JSON; use a .json filename.")
        atomic_json(path, default_config())
        return {"config": str(path), "routes": 6, "connectivity": "not_tested"}
    if args.command == "routes":
        return {"routes": list_routes(load_config(args.config))}
    if args.command == "install-info":
        try:
            edge_version = importlib.metadata.version("edge-tts")
        except importlib.metadata.PackageNotFoundError:
            edge_version = None
        return {"version": __version__, "python": sys.executable, "python_version": platform.python_version(),
                "package": str(Path(__file__).parent), "ffmpeg": shutil.which("ffmpeg"),
                "ffprobe": shutil.which("ffprobe"), "edge_tts": edge_version}
    project = Project(args.project)
    if args.command == "import":
        return project.import_asset(args.file, args.kind, args.select)
    if args.command == "storyboard":
        return project.put_storyboard(read_json(args.file))
    if args.command == "plan":
        return save_plan(project, read_json(args.file) if args.file else make_plan(project))
    if args.command in ("status", "ready"):
        result = status(project)
        if args.command == "ready":
            return {"project": str(project.root), "ready": [a for a in result["actions"] if a["ready"]],
                    "resumable": [a for a in result["actions"] if a["status"] in ("submitted", "waiting_download")]}
        return result
    if args.command == "run":
        return run(project, load_config(args.config), args.action, args.wait_seconds, args.retry, args.force)
    if args.command == "select":
        selected = project.select(args.slot, args.asset_ids)
        selected["state"] = status(project)
        return selected
    if args.command == "generate":
        request = read_json(args.request)
        if isinstance(request, dict) and request.get("kind") not in ("image", "video"):
            raise NTFError("request_kind", "generate accepts image or video; use ntf audio for speech.")
        aid = add_request(project, request)
        return run(project, load_config(args.config), aid, args.wait_seconds)
    if args.command == "audio":
        request = {"kind": "audio", "text": args.text, "voice": args.voice,
                   "rate": args.rate, "volume": args.volume, "pitch": args.pitch}
        for key, value in (("speech_text", args.speech_text), ("action_id", args.action_id), ("selection_slot", args.select)):
            if value is not None:
                request[key] = value
        return run(project, action_id=add_request(project, request))
    if args.command == "compose":
        return compose_project(project, read_json(args.manifest), render=not args.plan_only)
    raise NTFError("command", "Unknown command.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if any(x.get("error") or x.get("status") in ("failed", "unknown", "interrupted") for x in result.get("executed", [])) else 0
    except NTFError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": "local_operation_failed", "message": "Cannot read or write a local input.",
                                    "details": {"exception": type(exc).__name__}}}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "interrupted", "message": "Local execution stopped; use ntf status before resuming."}}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
