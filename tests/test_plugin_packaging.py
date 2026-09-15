from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from unittest.mock import Mock

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
    assert codex["version"] == claude["version"]
    assert re.fullmatch(r"0\.6\.0-dev\.0(?:\+codex\.[a-z0-9-]+)?", codex["version"])
    assert codex["skills"] == claude["skills"] == "./skills/"
    assert (PLUGIN / "skills" / "bello-delegate" / "SKILL.md").is_file()
    assert SCRIPT.is_file()
    advisor = PLUGIN / "skills" / "bello-config-advisor"
    assert (advisor / "SKILL.md").is_file()
    for script in ("inspect_models.py", "inspect_config.py", "validate_config.py"):
        assert (advisor / "scripts" / script).is_file()
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


def test_status_is_read_only_without_a_launch_and_marks_dead_process_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    def missing_process(pid, signal):
        raise ProcessLookupError("no such process")
    monkeypatch.setattr(launcher.os, "kill", missing_process)
    monkeypatch.setattr(launcher, "_windows_pid_alive", lambda pid: False)
    stale = launcher.status(project)
    assert stale["launcher"]["status"] == "stale"
    assert stale["launcher"]["belloProcessAliveUnverified"] is False


@pytest.mark.parametrize("recorded_status", ["launching", "running", "exited"])
def test_status_does_not_report_dead_run_when_process_probe_is_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_status: str,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    run_dir = project / ".codex" / "bello-run"
    run_dir.mkdir(parents=True)
    state = json.dumps({"status": recorded_status, "launcherPid": 1234, "pid": 5678})
    state_path = run_dir / "state.json"
    state_path.write_text(state, encoding="utf-8")

    def forbidden(pid, signal):
        raise PermissionError("sandbox forbids process inspection")
    monkeypatch.setattr(launcher.os, "kill", forbidden)
    monkeypatch.setattr(launcher, "_windows_pid_alive", lambda pid: None)

    observed = launcher.status(project)["launcher"]
    assert observed["status"] == recorded_status
    assert observed["launcherProcessAliveUnverified"] is None
    assert observed["belloProcessAliveUnverified"] is None
    assert state_path.read_text(encoding="utf-8") == state


@pytest.mark.parametrize("wait_result,open_error,expected", [
    (258, None, True), (0, None, False), (0xFFFFFFFF, None, None),
    (128, None, None), (None, 87, False), (None, 5, None), (None, 0, None),
])
def test_windows_liveness_is_a_query_only_zero_wait(
    monkeypatch: pytest.MonkeyPatch, wait_result, open_error, expected,
) -> None:
    import ctypes

    launcher = _load_launcher()
    kernel32 = Mock()
    kernel32.OpenProcess.return_value = 456 if open_error is None else 0
    kernel32.WaitForSingleObject.return_value = wait_result
    loader = Mock(return_value=kernel32)
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: open_error, raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "kill", lambda *a: pytest.fail("Windows must never signal the PID"))

    assert launcher._pid_alive(1234) is expected
    loader.assert_called_once_with("kernel32", use_last_error=True)
    kernel32.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
    if open_error is None:
        kernel32.WaitForSingleObject.assert_called_once_with(456, 0)
        kernel32.CloseHandle.assert_called_once_with(456)
    else:
        kernel32.WaitForSingleObject.assert_not_called()
        kernel32.CloseHandle.assert_not_called()


def test_windows_liveness_closes_handle_when_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    launcher = _load_launcher()
    kernel32 = Mock()
    kernel32.OpenProcess.return_value = 456
    kernel32.WaitForSingleObject.side_effect = OSError("query unavailable")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "kill", lambda *a: pytest.fail("Windows must never signal the PID"))

    assert launcher._pid_alive(1234) is None
    kernel32.CloseHandle.assert_called_once_with(456)


@pytest.mark.parametrize("recorded_status", ["launching", "running"])
def test_unknown_liveness_keeps_active_lock_duplicate_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_status: str,
) -> None:
    launcher = _load_launcher()
    run_dir = tmp_path / ".codex" / "bello-run"
    run_dir.mkdir(parents=True)
    state = json.dumps({"status": recorded_status, "launcherPid": 1234, "pid": 5678})
    (run_dir / "state.json").write_text(state, encoding="utf-8")
    (run_dir / "active.lock").write_text("existing lock", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: None)
    monkeypatch.setattr(launcher, "_git_status", lambda project: "")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("duplicate launched"))

    observed = launcher.start(tmp_path)
    assert observed["duplicateRejected"] is True
    assert observed["launcher"]["status"] == recorded_status
    assert (run_dir / "state.json").read_text(encoding="utf-8") == state
    assert (run_dir / "active.lock").read_text(encoding="utf-8") == "existing lock"


@pytest.mark.skipif(os.name != "nt", reason="actual Windows process-handle semantics")
def test_actual_windows_status_probe_preserves_live_child_and_detects_exit_259() -> None:
    import subprocess

    launcher = _load_launcher()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(3):
            assert launcher._pid_alive(child.pid) is True
            assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
    with subprocess.Popen([sys.executable, "-c", "raise SystemExit(259)"]) as exited:
        assert exited.wait(timeout=10) == 259
        assert launcher._pid_alive(exited.pid) is False


@pytest.mark.skipif(os.name == "nt", reason="the race fixture unlinks an open POSIX inode")
def test_launcher_json_read_rechecks_atomically_replaced_unlinked_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    path = tmp_path / "state.json"
    replacement = tmp_path / "replacement.json"
    path.write_text('{"status": "running"}', encoding="utf-8")
    replacement.write_text('{"status": "exited"}', encoding="utf-8")
    original_lstat = Path.lstat
    calls = 0

    with path.open("rb") as old_inode:
        def racing_lstat(candidate: Path):
            nonlocal calls
            if candidate == path:
                calls += 1
                if calls == 1:
                    os.replace(replacement, path)
                    old_info = os.fstat(old_inode.fileno())
                    assert old_info.st_nlink == 0
                    assert old_info.st_ino != original_lstat(path).st_ino
                    return old_info
            return original_lstat(candidate)

        monkeypatch.setattr(Path, "lstat", racing_lstat)
        assert launcher._read_json(path) == {"status": "exited"}
    assert calls == 2


@pytest.mark.parametrize(
    ("next_mode", "next_links", "next_inode", "expected_calls", "message"),
    [
        (stat.S_IFREG | 0o600, 0, 10, 3, "ordinary, unshared"),
        (stat.S_IFREG | 0o600, 1, 10, 2, "ordinary, unshared"),
        (stat.S_IFREG | 0o600, 2, 11, 2, "ordinary, unshared"),
        (stat.S_IFDIR | 0o700, 1, 11, 2, "ordinary, unshared"),
        (stat.S_IFLNK | 0o700, 1, 11, 2, "symbolic link"),
    ],
)
def test_launcher_unlinked_inode_retry_stays_bounded_and_rejects_unsafe_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_mode: int,
    next_links: int,
    next_inode: int,
    expected_calls: int,
    message: str,
) -> None:
    launcher = _load_launcher()
    path = tmp_path / "state.json"
    unlinked = os.stat_result((stat.S_IFREG | 0o600, 10, 1, 0, 0, 0, 0, 0, 0, 0))
    replacement = os.stat_result((next_mode, next_inode, 1, next_links, 0, 0, 0, 0, 0, 0))
    calls = 0
    original_lstat = Path.lstat

    def racing_lstat(candidate: Path):
        nonlocal calls
        if candidate != path:
            return original_lstat(candidate)
        calls += 1
        return unlinked if calls == 1 else replacement

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    with pytest.raises(launcher.LauncherError, match=message):
        launcher._safe_regular_file(path, path.name)
    assert calls == expected_calls


@pytest.mark.skipif(os.name == "nt", reason="the test fixture uses a POSIX shebang")
def test_start_runs_pipx_bello_symlink_in_background_and_reports_durable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    executable = tmp_path / "home" / ".local" / "share" / "pipx" / "venvs" / "bello" / "bin" / "bello"
    executable.parent.mkdir(parents=True)
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
    (bin_dir / "bello").symlink_to(executable)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))

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


@pytest.mark.parametrize("absolute_command", [False, True], ids=["path-lookup", "absolute-command"])
@pytest.mark.parametrize("relative_target", [False, True], ids=["absolute-link", "relative-link"])
def test_posix_lookup_accepts_pipx_executable_symlink(
    tmp_path: Path, absolute_command: bool, relative_target: bool,
) -> None:
    launcher = _load_launcher()
    project, current = tmp_path / "project", tmp_path / "current"
    project.mkdir()
    current.mkdir()
    bin_dir = tmp_path / "home" / ".local" / "bin"
    target = tmp_path / "home" / ".local" / "share" / "pipx" / "venvs" / "bello" / "bin" / "bello"
    bin_dir.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_text("placeholder", encoding="utf-8")
    target.chmod(0o755)
    link = bin_dir / "bello"
    link.symlink_to(os.path.relpath(target, bin_dir) if relative_target else target)

    assert launcher._resolve_scoped_executable(
        str(link) if absolute_command else "bello", project,
        environ={"PATH": str(bin_dir)}, cwd=current, windows=False,
    ) == target.resolve()


@pytest.mark.parametrize("location", ["project", "cwd"])
@pytest.mark.parametrize("direction", ["origin", "target"])
@pytest.mark.parametrize("absolute_command", [False, True], ids=["path-lookup", "absolute-command"])
def test_executable_symlink_cannot_origin_or_resolve_inside_blocked_scope(
    tmp_path: Path, location: str, direction: str, absolute_command: bool,
) -> None:
    launcher = _load_launcher()
    project, current, trusted = tmp_path / "project", tmp_path / "current", tmp_path / "trusted"
    for directory in (project, current, trusted):
        directory.mkdir()
    blocked = project if location == "project" else current
    origin = blocked if direction == "origin" else trusted
    destination = trusted if direction == "origin" else blocked
    target = destination / "real-bello"
    target.write_text("placeholder", encoding="utf-8")
    target.chmod(0o755)
    link = origin / "bello"
    link.symlink_to(target)

    assert launcher._resolve_scoped_executable(
        str(link) if absolute_command else "bello", project,
        environ={"PATH": str(origin)}, cwd=current, windows=False,
    ) is None


@pytest.mark.parametrize("kind", ["broken", "cycle", "directory", "non-executable"])
def test_posix_lookup_rejects_invalid_symlink_target(tmp_path: Path, kind: str) -> None:
    if os.name == "nt" and kind == "non-executable":
        pytest.skip("POSIX execute-bit check")
    launcher = _load_launcher()
    project, current, trusted = tmp_path / "project", tmp_path / "current", tmp_path / "trusted"
    for directory in (project, current, trusted):
        directory.mkdir()
    target, link = trusted / "target", trusted / "bello"
    if kind == "cycle":
        target.symlink_to(link)
    elif kind == "directory":
        target.mkdir()
    elif kind == "non-executable":
        target.write_text("placeholder", encoding="utf-8")
        target.chmod(0o644)
    link.symlink_to(target, target_is_directory=kind == "directory")

    assert launcher._resolve_scoped_executable(
        "bello", project, environ={"PATH": str(trusted)}, cwd=current, windows=False,
    ) is None


@pytest.mark.parametrize("link_kind", ["executable", "ancestor"])
def test_windows_lookup_keeps_symlink_and_reparse_ancestor_rejection(
    tmp_path: Path, link_kind: str,
) -> None:
    launcher = _load_launcher()
    project, current, trusted, alias = (
        tmp_path / name for name in ("project", "current", "trusted", "alias")
    )
    for directory in (project, current, trusted):
        directory.mkdir()
    target = trusted / "real.EXE"
    target.write_text("placeholder", encoding="utf-8")
    if link_kind == "executable":
        (trusted / "bello.EXE").symlink_to(target)
        search_dir = trusted
    else:
        (trusted / "bello.EXE").write_text("placeholder", encoding="utf-8")
        alias.symlink_to(trusted, target_is_directory=True)
        search_dir = alias

    assert launcher._resolve_scoped_executable(
        "bello", project, environ={"PATH": str(search_dir), "PATHEXT": ".EXE"},
        cwd=current, windows=True,
    ) is None


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
