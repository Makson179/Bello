from __future__ import annotations

import os
from pathlib import Path

import pytest

import supervisor.appserver as appserver_module
import supervisor.executables as executables_module
from supervisor.appserver import AppServerError
from supervisor.executables import resolve_trusted_executable


def _touch(path: Path, content: str = "fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_windows_resolver_skips_relative_and_workspace_path_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    trusted = tmp_path / "trusted-bin"
    workspace.mkdir()
    trusted.mkdir()
    _touch(workspace / "codex.EXE", "workspace")
    expected = _touch(trusted / "codex.EXE", "trusted")
    environ = {
        "PATH": os.pathsep.join([".", str(workspace), str(trusted)]),
        "PATHEXT": ".EXE;.CMD",
    }

    resolved = resolve_trusted_executable(
        "codex",
        cwd=workspace,
        environ=environ,
        windows=True,
    )

    assert resolved == str(expected.resolve())


def test_windows_resolver_accepts_balanced_quoted_absolute_path_entry(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "trusted bin"
    expected = _touch(trusted / "codex.EXE")

    resolved = resolve_trusted_executable(
        "codex",
        cwd=tmp_path / "workspace",
        environ={"PATH": f'"{trusted}"', "PATHEXT": ".EXE"},
        windows=True,
    )

    assert resolved == str(expected.resolve())


def test_windows_resolver_fails_closed_when_only_candidate_is_in_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = _touch(workspace / "git.EXE")
    environ = {"PATH": str(workspace), "PATHEXT": ".EXE"}

    assert (
        resolve_trusted_executable(
            "git",
            cwd=workspace,
            environ=environ,
            windows=True,
        )
        is None
    )
    assert (
        resolve_trusted_executable(
            str(candidate),
            cwd=workspace,
            environ=environ,
            windows=True,
        )
        is None
    )


def test_windows_resolver_rejects_reparse_candidate(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    target = _touch(tmp_path / "real" / "codex.EXE")
    candidate = trusted / "codex.EXE"
    try:
        candidate.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert (
        resolve_trusted_executable(
            "codex",
            cwd=tmp_path / "workspace",
            environ={"PATH": str(trusted), "PATHEXT": ".EXE"},
            windows=True,
        )
        is None
    )


def test_appserver_windows_command_never_falls_back_to_bare_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _touch(workspace / "codex.EXE")
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    with pytest.raises(AppServerError, match="trusted executable"):
        appserver_module._app_server_command(
            ["codex", "app-server"],
            cwd=workspace,
            environ={"PATH": os.pathsep.join([".", str(workspace)]), "PATHEXT": ".EXE"},
        )


def test_posix_resolver_retains_normal_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def which(command: str, path: str | None = None) -> str | None:
        calls.append((command, path))
        return "/trusted/bin/tool"

    monkeypatch.setattr(executables_module.shutil, "which", which)

    assert resolve_trusted_executable(
        "tool",
        environ={"PATH": "/trusted/bin"},
        windows=False,
    ) == "/trusted/bin/tool"
    assert calls == [("tool", "/trusted/bin")]
