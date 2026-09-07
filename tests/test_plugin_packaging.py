from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "bello"
SCRIPT = PLUGIN / "skills" / "bello-delegate" / "scripts" / "bello_delegate.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("bello_plugin_launcher", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_and_claude_manifests_share_one_versioned_skill() -> None:
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert codex["name"] == claude["name"] == "bello"
    assert codex["version"] == claude["version"] == "0.6.0-dev.0"
    assert codex["skills"] == claude["skills"] == "./skills/"
    assert (PLUGIN / "skills" / "bello-delegate" / "SKILL.md").is_file()
    assert SCRIPT.is_file()
    assert not (PLUGIN / "skills" / "bello-config-advisor").exists()
    assert not list(PLUGIN.rglob("marketplace.json"))


def test_skill_uses_saved_configuration_without_frontend_model_inference() -> None:
    skill = (PLUGIN / "skills" / "bello-delegate" / "SKILL.md").read_text(encoding="utf-8")

    assert ".supervisor/config.json" in skill
    assert "frontend agent and its model do not choose" in skill
    assert "Do not infer or pass any model" in skill
    assert "plugin_self_update" not in skill
    assert "--super-mod" not in skill


def test_default_command_is_only_bello_and_explicit_files_add_only_task_and_plan(
    tmp_path: Path,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "bello executable"
    executable.write_text("placeholder", encoding="utf-8")
    task = project / "TASK.md"
    plan = project / "PLAN.md"
    task.write_text("task", encoding="utf-8")
    plan.write_text("plan", encoding="utf-8")

    assert launcher._build_bello_command(project, executable=executable) == [str(executable)]
    command = launcher._build_bello_command(
        project,
        task="TASK.md",
        plan="PLAN.md",
        executable=executable,
    )
    assert command == [
        str(executable),
        "--task",
        str(task),
        "--plan",
        str(plan),
    ]
    assert not any("model" in part or "intelligence" in part for part in command)


def test_task_and_plan_cannot_escape_project(tmp_path: Path) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(launcher.LauncherError, match="inside the project"):
        launcher._build_bello_command(
            project,
            task=str(outside),
            executable=tmp_path / "bello",
        )


def test_status_is_read_only_without_a_launch_and_marks_dead_process_stale(tmp_path: Path) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()

    missing = launcher.status(project)
    assert missing["launcher"]["status"] == "not_launched"
    assert not (project / ".codex").exists()

    run_dir = project / ".codex" / "bello-run"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps({"status": "running", "pid": 2_147_483_647}),
        encoding="utf-8",
    )
    stale = launcher.status(project)
    assert stale["launcher"]["status"] == "stale"
    assert stale["launcher"]["belloProcessAliveUnverified"] is False


@pytest.mark.skipif(os.name == "nt", reason="the test fixture uses a POSIX shebang")
def test_start_runs_fake_bello_in_background_and_reports_durable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "fake bello"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "state = Path.cwd() / '.supervisor'\n"
        "state.mkdir()\n"
        "(state / 'config.json').write_text(json.dumps({'status': 'complete'}))\n"
        "(state / 'FINAL_REPORT.md').write_text('# Final Report\\n\\n- Status: complete\\n')\n"
        "print('fake Bello completed')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setattr(launcher, "_find_bello", lambda _project: executable.resolve())

    initial = launcher.start(project)
    assert initial["launcher"]["status"] in {"launching", "running", "exited"}

    deadline = time.monotonic() + 5
    while True:
        observed = launcher.status(project)
        if observed["launcher"]["status"] in launcher.TERMINAL_STATES:
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"fake Bello did not exit: {observed}")
        time.sleep(0.05)

    assert observed["launcher"]["status"] == "exited"
    assert observed["launcher"]["exitCode"] == 0
    assert observed["launcher"]["command"] == [str(executable.resolve())]
    assert observed["supervisor"]["status"] == "complete"
    assert "fake Bello completed" in observed["artifacts"]["stdout"]
    assert "Status: complete" in observed["artifacts"]["finalReport"]
    assert not (project / ".codex" / "bello-run" / "active.lock").exists()
    assert sys.executable not in observed["launcher"]["command"]


def test_windows_lookup_skips_relative_project_and_current_directory_entries(
    tmp_path: Path,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    current = tmp_path / "current"
    trusted = tmp_path / "trusted" / "bin"
    project.mkdir()
    current.mkdir()
    trusted.mkdir(parents=True)
    for directory in (project, current, trusted):
        (directory / "bello.EXE").write_text("placeholder", encoding="utf-8")

    resolved = launcher._resolve_scoped_executable(
        "bello",
        project,
        environ={
            "PATH": os.pathsep.join([".", str(project), str(current), str(trusted)]),
            "PATHEXT": ".EXE",
        },
        cwd=current,
        windows=True,
    )

    assert resolved == (trusted / "bello.EXE").resolve()
    assert resolved.is_absolute()


@pytest.mark.skipif(os.name == "nt", reason="the test fixture uses a POSIX shebang")
def test_git_status_does_not_launch_project_path_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "workspace-git-ran"
    fake_git = project / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf unsafe > {marker}\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(project))

    assert launcher._git_status(project) == ""
    assert not marker.exists()
