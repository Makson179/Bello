from __future__ import annotations

import os
import ntpath
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervisor.appserver as appserver_module
import supervisor.executables as executables_module
from supervisor.appserver import AppServerError
from supervisor.executables import resolve_trusted_executable


@pytest.mark.parametrize(("candidate", "workspace", "blocked"), [
    (r"C:\hostedtoolcache\Python\python.exe", r"D:\a\project", False),
    (r"D:\trusted-bin\node.exe", r"C:\workspace", False),
    (r"\\server\tools\node.exe", r"\\server\workspace\project", False),
    (r"D:\a\project\node.exe", r"D:\a\project", True),
    (r"d:\A\PROJECT\node.exe", r"D:\a\project", True),
    (r"D:\a\project-sibling\node.exe", r"D:\a\project", False),
    (r"C:relative.exe", r"D:\a\project", True),
    (r"C:\trusted\node.exe", r"D:relative-workspace", True),
    (r"relative.exe", r"relative-workspace", True),
])
def test_windows_canonical_containment_handles_distinct_drives(
    candidate, workspace, blocked, monkeypatch,
):
    # Exercise Windows path semantics on every host; these stand-ins are the
    # already-resolved paths consumed by the containment check, not executables.
    canonical = lambda value: SimpleNamespace(resolve=lambda strict=False: value)
    monkeypatch.setattr(executables_module, "os", SimpleNamespace(path=ntpath))
    assert executables_module._path_is_blocked(
        canonical(candidate), [canonical(workspace)],
    ) is blocked


@pytest.mark.parametrize("broken_side", ["candidate", "workspace"])
def test_canonical_containment_rejects_unresolvable_paths(broken_side, monkeypatch):
    def invalid(*, strict=False):
        raise ValueError("ambiguous path")
    candidate = SimpleNamespace(resolve=lambda strict=False: r"C:\trusted\node.exe")
    workspace = SimpleNamespace(resolve=lambda strict=False: r"D:\a\project")
    (candidate if broken_side == "candidate" else workspace).resolve = invalid
    monkeypatch.setattr(executables_module, "os", SimpleNamespace(path=ntpath))
    assert executables_module._path_is_blocked(candidate, [workspace])


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


def test_windows_resolver_launches_canonical_target_below_linked_parent(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical-bin"
    expected = _touch(canonical / "python.EXE")
    linked = tmp_path / "linked-bin"
    try:
        linked.symlink_to(canonical, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    resolved = resolve_trusted_executable(
        str(linked / "python.EXE"),
        cwd=tmp_path / "workspace",
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
