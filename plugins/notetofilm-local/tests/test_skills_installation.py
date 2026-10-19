"""Skill packaging and isolated installation tests; no company APIs are called."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import tempfile
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[1]
NAMES = tuple(f"notetofilm-{name}" for name in ("workflow", "prepare", "direct", "image", "video", "audio", "compose"))


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"ntf_{name}_test", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_source(tmp_path: Path) -> Path:
    """A clearly labelled miniature runtime tests installation, not video capability."""
    source = tmp_path / "source"
    source.mkdir()
    for filename in ("plugin.json", "pyproject.toml", "README.md"):
        shutil.copyfile(ROOT / filename, source / filename)
    for directory in ("skills", "shared", "scripts"):
        shutil.copytree(ROOT / directory, source / directory, ignore=shutil.ignore_patterns("__pycache__"))
    package = source / "runtime/notetofilm_local"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (package / "__main__.py").write_text("from .cli import main\nraise SystemExit(main())\n", encoding="utf-8")
    (package / "cli.py").write_text(
        'import json, pathlib, sys\ndef main():\n print(json.dumps({"fixture": True, "package": str(pathlib.Path(__file__).parent), "prefix": sys.prefix}))\n return 0\n',
        encoding="utf-8",
    )
    return source


def run_install(source: Path, base: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(source / "scripts/install.py"), "--skills-dir", str(base / "skills"), "--runtime-dir", str(base / "runtime"), *extra],
        text=True, capture_output=True, check=False,
    )


class SkillsInstallationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ntf-skills-test-")
        self.addCleanup(self.directory.cleanup)
        self.tmp_path = Path(self.directory.name)

    def test_seven_skills_have_valid_metadata_and_self_contained_references(self):
        assert {path.name for path in (ROOT / "skills").iterdir() if path.is_dir()} == set(NAMES)
        for name in NAMES:
            directory = ROOT / "skills" / name
            text = (directory / "SKILL.md").read_text(encoding="utf-8")
            front = yaml.safe_load(text.split("---", 2)[1])
            assert front["name"] == name
            assert set(front) == {"name", "description"}
            assert front["description"] and "TODO" not in text
            ui = yaml.safe_load((directory / "agents/openai.yaml").read_text())["interface"]
            assert 25 <= len(ui["short_description"]) <= 64
            assert f"${name}" in ui["default_prompt"]
            for reference in re.findall(r"\]\(([^)]+)\)", text):
                assert not reference.startswith(("/", "../", "http"))
                resolved = (directory / reference).resolve()
                assert resolved.is_relative_to(directory.resolve()) and resolved.is_file()
            for common in ("runtime.md", "commands.md", "project-format.md"):
                assert (directory / "references" / common).read_bytes() == (ROOT / "shared" / common).read_bytes()


    def test_portable_manifest_matches_python_package(self):
        import tomllib
        manifest = json.loads((ROOT / "plugin.json").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        assert manifest["name"] == project["name"] == "notetofilm-local"
        assert manifest["version"] == project["version"]
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
        assert not {"skills", "interface", "mcpServers"}.intersection(manifest)
        interface = manifest["extensions"]["com.openai"]["interface"]
        assert 0 < len(interface["shortDescription"]) <= 30
        assert isinstance(interface["capabilities"], list)


    @unittest.skipIf(os.name == "nt", "This subprocess proof covers POSIX launchers; Windows requires native execution")
    def test_isolated_install_survives_source_removal_and_preserves_other_skills(self):
        tmp_path = self.tmp_path
        source = fixture_source(tmp_path)
        base = tmp_path / "installed with spaces"
        unrelated = base / "skills/unrelated/SKILL.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("existing skill", encoding="utf-8")
        completed = run_install(source, base)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        installed = json.loads(completed.stdout)
        reference = (base / "skills/notetofilm-workflow/references/runtime.md").read_text()
        locator = json.loads(reference.split("```json\n", 1)[1].split("\n```", 1)[0])
        assert locator["ntf_command"] == installed["ntf_command"]
        assert set(locator["skill_locations"]) == set(NAMES)
        shutil.rmtree(source)
        command = subprocess.run([installed["ntf_command"], "install-info"], cwd=tmp_path, text=True, capture_output=True, check=True)
        result = json.loads(command.stdout)
        assert result["fixture"] is True
        assert Path(result["package"]).is_relative_to(base / "runtime")
        assert Path(result["prefix"]) == base / "runtime/venv"
        assert unrelated.read_text() == "existing skill"
        assert all((Path(path) / "SKILL.md").is_file() for path in locator["skill_locations"].values())


    def test_conflicting_skill_stops_before_creating_runtime(self):
        tmp_path = self.tmp_path
        source = fixture_source(tmp_path)
        base = tmp_path / "installed"
        conflict = base / "skills/notetofilm-image/SKILL.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("belongs to another plugin", encoding="utf-8")
        completed = run_install(source, base, "--upgrade")
        assert completed.returncode == 1
        assert conflict.read_text() == "belongs to another plugin"
        assert not (base / "runtime").exists()
        assert not (base / "skills/notetofilm-workflow").exists()


    @unittest.skipIf(os.name == "nt", "This subprocess proof covers POSIX launchers")
    def test_failed_upgrade_restores_previous_runtime_and_skills(self):
        tmp_path = self.tmp_path
        source = fixture_source(tmp_path)
        base = tmp_path / "installed"
        first = run_install(source, base)
        assert first.returncode == 0, first.stdout + first.stderr
        command = json.loads(first.stdout)["ntf_command"]
        prior = (base / "skills/notetofilm-workflow/SKILL.md").read_bytes()
        (source / "runtime/notetofilm_local/cli.py").write_text('raise RuntimeError("fixture startup failure")\n')
        changed = source / "skills/notetofilm-workflow/SKILL.md"
        changed.write_text(changed.read_text() + "\nFixture replacement that must be rolled back.\n")
        failed = run_install(source, base, "--upgrade")
        assert failed.returncode == 1
        assert (base / "skills/notetofilm-workflow/SKILL.md").read_bytes() == prior
        restored = subprocess.run([command, "install-info"], text=True, capture_output=True, check=True)
        assert json.loads(restored.stdout)["fixture"] is True


    def test_private_archive_has_one_root_and_omits_local_configuration(self):
        tmp_path = self.tmp_path
        source = fixture_source(tmp_path)
        (source / "config").mkdir()
        (source / "config/company.example.json").write_text('{"fixture":true}')
        (source / "docs").mkdir()
        (source / "docs/protocol.md").write_text("Fixture protocol documentation")
        (source / "company-config.json").write_text('{"local_only":true}')
        (source / ".env").write_text("LOCAL_ONLY=fixture\n")
        output = tmp_path / "release.zip"
        package = load_script("package")
        result = package.build(source, output)
        assert result["status"] == "packaged"
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            assert all(name.startswith("notetofilm-local/") for name in names)
            assert not any(".." in Path(name).parts for name in names)
            assert "notetofilm-local/plugin.json" in names
            assert "notetofilm-local/runtime/notetofilm_local/cli.py" in names
            assert "notetofilm-local/config/company.example.json" in names
            assert "notetofilm-local/docs/protocol.md" in names
            assert not any(name.endswith(("company-config.json", "/.env")) for name in names)
            assert sum(name.endswith("/SKILL.md") for name in names) == 7
        with self.assertRaisesRegex(ValueError, "outside"):
            package.build(source, source / "bad.zip")


if __name__ == "__main__":
    unittest.main()
