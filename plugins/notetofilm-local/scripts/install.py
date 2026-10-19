#!/usr/bin/env python3
"""Install this skill suite into explicitly selected, isolated local directories."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv


PLUGIN_NAME = "notetofilm-local"
SKILL_NAMES = (
    "notetofilm-workflow", "notetofilm-prepare", "notetofilm-direct",
    "notetofilm-image", "notetofilm-video", "notetofilm-audio",
    "notetofilm-compose",
)
SKILL_MARKER = Path("agents/notetofilm-install.json")
RUNTIME_MARKER = Path("installation.json")
SOURCE_ROOT = Path(__file__).resolve().parent.parent


class InstallError(Exception):
    pass


def json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def absolute_path(value: str) -> Path:
    # Preserve the final path component so a symlink is not mistaken for an owned directory.
    path = Path(value).expanduser().absolute()
    return path.parent.resolve() / path.name


def owned_target(path: Path, marker: Path, upgrade: bool) -> None:
    if path.is_symlink():
        raise InstallError(f"Refusing to replace a symbolic-link target: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise InstallError(f"Install target is not a directory: {path}")
    if not any(path.iterdir()):
        return
    try:
        record = json.loads((path / marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InstallError(f"Existing directory is not an installation of this plugin: {path}") from None
    if record.get("plugin") != PLUGIN_NAME:
        raise InstallError(f"Existing directory belongs to another installation: {path}")
    if not upgrade:
        raise InstallError(f"Plugin is already installed at {path}; use --upgrade to replace this installation")


def reject_source_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise InstallError(f"Source component contains a symbolic link: {path.relative_to(SOURCE_ROOT)}")


def ignore_generated(_path: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {"__pycache__", ".pytest_cache", ".DS_Store"} or name.endswith((".pyc", ".pyo"))}


def run_checked(args: list[str], label: str) -> str:
    completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode:
        # Do not echo package-index URLs or environment-derived connection strings.
        raise InstallError(f"{label} failed (exit {completed.returncode})")
    return completed.stdout.strip()


def write_runtime_reference(skill: Path, command: Path, python: Path, locations: dict[str, str], version: str) -> None:
    source = (SOURCE_ROOT / "shared/runtime.md").read_text(encoding="utf-8")
    data = {
        "plugin": PLUGIN_NAME,
        "version": version,
        "ntf_command": str(command),
        "python_module_argv": [str(python), "-I", "-m", "notetofilm_local"],
        "skill_locations": locations,
    }
    text = "# 已安装的本机入口\n\n使用下面的实际位置。调用时把命令与参数作为独立参数传入。\n\n```json\n"
    text += json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n\n"
    text += source.split("\n", 1)[1]
    (skill / "references/runtime.md").write_text(text, encoding="utf-8")


def install(skills_dir: Path, runtime_dir: Path, *, upgrade: bool, with_audio: bool) -> dict:
    if sys.version_info < (3, 11):
        raise InstallError("Python 3.11 or later is required")
    if skills_dir == runtime_dir or skills_dir in runtime_dir.parents or runtime_dir in skills_dir.parents:
        raise InstallError("Use separate, non-nested skills and runtime directories")
    for target in (skills_dir, runtime_dir):
        if target == SOURCE_ROOT or target in SOURCE_ROOT.parents or SOURCE_ROOT in target.parents:
            raise InstallError("Installation targets must be separate from the plugin source directory")
    if skills_dir.is_symlink() or (skills_dir.exists() and not skills_dir.is_dir()):
        raise InstallError("The skills directory must be a real directory")
    manifest = json.loads((SOURCE_ROOT / "plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME:
        raise InstallError("Unexpected source plugin name")
    version = manifest["version"]
    package_source = SOURCE_ROOT / "runtime/notetofilm_local"
    for required in ("__init__.py", "__main__.py", "cli.py"):
        if not (package_source / required).is_file():
            raise InstallError(f"Runtime source is incomplete: {required}")
    reject_source_symlinks(package_source)
    targets = [(runtime_dir, RUNTIME_MARKER)]
    for name in SKILL_NAMES:
        source = SOURCE_ROOT / "skills" / name
        if not (source / "SKILL.md").is_file():
            raise InstallError(f"Missing skill source: {name}")
        reject_source_symlinks(source)
        targets.append((skills_dir / name, SKILL_MARKER))
    for target, marker in targets:
        owned_target(target, marker, upgrade)

    backups: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        for target, _marker in targets:
            if target.exists():
                backup = Path(tempfile.mkdtemp(prefix=f".{target.name}-previous-", dir=target.parent))
                target.rename(backup / "contents")
                backups.append((target, backup))

        runtime_dir.mkdir()
        created.append(runtime_dir)
        env_dir = runtime_dir / "venv"
        venv.EnvBuilder(with_pip=with_audio, symlinks=os.name != "nt").create(env_dir)
        python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        purelib = Path(run_checked([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], "Reading the isolated Python environment"))
        if not purelib.is_relative_to(runtime_dir):
            raise InstallError("The selected Python environment did not resolve inside the runtime directory")
        purelib.mkdir(parents=True, exist_ok=True)
        installed_package = purelib / "notetofilm_local"
        shutil.copytree(package_source, installed_package, ignore=ignore_generated)
        info_dir = purelib / f"notetofilm_local-{version}.dist-info"
        info_dir.mkdir()
        (info_dir / "METADATA").write_text(f"Metadata-Version: 2.1\nName: notetofilm-local\nVersion: {version}\nRequires-Python: >=3.11\n", encoding="utf-8")
        (info_dir / "INSTALLER").write_text("notetofilm-local-isolated-installer\n", encoding="utf-8")
        (info_dir / "entry_points.txt").write_text("[console_scripts]\nntf = notetofilm_local.cli:main\n", encoding="utf-8")
        # This environment is managed as a complete directory by this installer.
        (info_dir / "RECORD").write_text("", encoding="utf-8")

        bin_dir = runtime_dir / "bin"
        bin_dir.mkdir()
        if os.name == "nt":
            command = bin_dir / "ntf.cmd"
            command.write_text(f'@"{python}" -I -m notetofilm_local %*\r\n', encoding="utf-8")
        else:
            command = bin_dir / "ntf"
            command.write_text(f'#!/bin/sh\nexec {shlex.quote(str(python))} -I -m notetofilm_local "$@"\n', encoding="utf-8")
            command.chmod(0o755)
        if with_audio:
            run_checked([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "edge-tts>=7.2,<8"], "Installing Edge TTS in the isolated environment")

        locations = {name: str(skills_dir / name) for name in SKILL_NAMES}
        record = {"plugin": PLUGIN_NAME, "version": version, "runtime_dir": str(runtime_dir), "ntf_command": str(command)}
        for name in SKILL_NAMES:
            target = skills_dir / name
            created.append(target)
            shutil.copytree(SOURCE_ROOT / "skills" / name, target, ignore=ignore_generated)
            write_runtime_reference(target, command, python, locations, version)
            json_write(target / SKILL_MARKER, record)
        json_write(runtime_dir / RUNTIME_MARKER, {**record, "skills_dir": str(skills_dir), "skills": list(SKILL_NAMES), "with_audio": with_audio})
        # This checks the installed code, not the source checkout.
        run_checked([str(python), "-I", "-m", "notetofilm_local", "--help"], "Starting the installed runtime")
    except Exception:
        for target in reversed(created):
            if target.exists():
                shutil.rmtree(target)
        for target, backup in reversed(backups):
            (backup / "contents").rename(target)
            backup.rmdir()
        raise
    for _target, backup in backups:
        shutil.rmtree(backup)
    return {"status": "installed", **record, "skills_dir": str(skills_dir), "skills": list(SKILL_NAMES), "with_audio": with_audio, "client_registration": "skills_directory_only"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", required=True, help="Explicit skill discovery directory; unrelated entries are preserved")
    parser.add_argument("--runtime-dir", required=True, help="Separate directory for this plugin's isolated Python environment")
    parser.add_argument("--with-audio", action="store_true", help="Install Edge TTS in the isolated environment (requires package-source access)")
    parser.add_argument("--upgrade", action="store_true", help="Replace only directories already owned by this plugin")
    args = parser.parse_args(argv)
    try:
        result = install(absolute_path(args.skills_dir), absolute_path(args.runtime_dir), upgrade=args.upgrade, with_audio=args.with_audio)
    except (InstallError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": "installation_failed", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
