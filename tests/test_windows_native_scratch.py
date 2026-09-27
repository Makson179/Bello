from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import subprocess

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime import codex
from supervisor.workspace_snapshot import create_workspace_snapshot, apply_snapshot_patch


def make_backend(tmp_path, monkeypatch, work=None):
    monkeypatch.setattr(codex, "_IS_WINDOWS", True)
    monkeypatch.setattr(codex, "native_toolchain_read_paths", lambda cwd: ())
    source = tmp_path / "source-home"
    source.mkdir(exist_ok=True, mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(source))
    work = work or tmp_path / "workspace"
    work.mkdir(exist_ok=True)
    backend = codex.CodexBackend(state_dir=tmp_path / "state", emit=AsyncMock())
    (backend.state_dir / "codex-home").mkdir(exist_ok=True, mode=0o700)
    return backend, work


def automatic_scratch(backend, work):
    params = {"cwd": str(work), "model": "gpt-6-sol", "sandbox": "workspace-write",
              "windowsNativeRootRead": True}
    before = deepcopy(params)
    mapped = backend._thread_params(params)
    assert params == before  # A removed transient path must not enter the journal.
    scratch = Path(mapped["config"]["shell_environment_policy"]["set"]["TMP"])
    assert scratch.parent == work / "__pycache__"
    return scratch


@pytest.mark.asyncio
async def test_cleanup_after_native_stop_preserves_parent_user_files(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    parent = work / "__pycache__"
    parent.mkdir()
    user_file = parent / "existing.pyc"
    user_file.write_bytes(b"existing user data")
    scratch = automatic_scratch(backend, work)
    (scratch / "ordinary.txt").write_text("tool output")
    assert automatic_scratch(backend, work) == scratch
    async def native_stop():
        assert scratch.is_dir()
    backend._client = SimpleNamespace(stop=AsyncMock(side_effect=native_stop))
    await backend.stop()
    backend._client.stop.assert_awaited_once()
    assert not scratch.exists()
    assert parent.is_dir() and user_file.read_bytes() == b"existing user data"
    await backend.stop()
    assert user_file.exists()


@pytest.mark.asyncio
async def test_failed_native_stop_does_not_delete_scratch(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    scratch = automatic_scratch(backend, work)
    backend._client = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("not stopped")))
    with pytest.raises(RuntimeError, match="not stopped"):
        await backend.stop()
    assert scratch.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
async def test_replaced_owned_leaf_is_not_deleted(tmp_path, monkeypatch, replacement):
    backend, work = make_backend(tmp_path, monkeypatch)
    scratch = automatic_scratch(backend, work)
    retained = scratch.with_name(scratch.name + "-retained")
    scratch.rename(retained)  # Keep original inode allocated.
    if replacement == "directory":
        scratch.mkdir()
        marker = scratch / "foreign.txt"
    else:
        target = tmp_path / "foreign"
        target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
        marker = target / "foreign.txt"
    marker.write_text("untouched")
    with pytest.raises(AppServerError, match="scratch"):
        await backend.stop()
    assert marker.read_text() == "untouched" and retained.is_dir()


@pytest.mark.asyncio
async def test_cleanup_helper_cannot_adopt_replacement_after_last_check(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    scratch = automatic_scratch(backend, work)
    cleanup = codex.remove_path_tree
    def raced_cleanup(path, *, stat_result):
        path.rename(path.with_name(path.name + "-retained"))
        path.mkdir()
        (path / "foreign.txt").write_text("untouched")
        cleanup(path, stat_result=stat_result)
    monkeypatch.setattr(codex, "remove_path_tree", raced_cleanup)
    with pytest.raises(OSError, match="changed|redirected"):
        await backend.stop()
    assert (scratch / "foreign.txt").read_text() == "untouched"


@pytest.mark.asyncio
async def test_replaced_parent_is_not_traversed_or_deleted(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    scratch = automatic_scratch(backend, work)
    parent = scratch.parent
    parent.rename(work / "retained-cache")
    parent.mkdir()
    foreign = parent / scratch.name
    foreign.mkdir()
    marker = foreign / "foreign.txt"
    marker.write_text("untouched")
    with pytest.raises(AppServerError, match="scratch"):
        await backend.stop()
    assert marker.read_text() == "untouched"


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_nonordinary_parent_is_rejected_without_touching_target(tmp_path, monkeypatch, kind):
    backend, work = make_backend(tmp_path, monkeypatch)
    parent = work / "__pycache__"
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "foreign.txt"
    marker.write_text("untouched")
    if kind == "file":
        parent.write_text("existing")
    else:
        parent.symlink_to(target, target_is_directory=True)
    with pytest.raises((AppServerError, OSError)):
        automatic_scratch(backend, work)
    assert marker.read_text() == "untouched"
    assert list(target.iterdir()) == [marker] and not backend._windows_scratch


@pytest.mark.asyncio
async def test_explicit_caller_scratch_not_owned_or_deleted(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    explicit = work / "explicit-review-scratch"
    explicit.mkdir()
    params = {"cwd": str(work), "model": "gpt-6-sol", "sandbox": "workspace-write",
              "windowsNativeRootRead": True, "runtimeScratchRoot": str(explicit)}
    before = deepcopy(params)
    native = backend._thread_params(params)
    assert params == before
    assert native["config"]["shell_environment_policy"]["set"]["TMP"] == str(explicit)
    assert not backend._windows_scratch_owned
    await backend.stop()
    assert explicit.is_dir()


@pytest.mark.asyncio
async def test_new_backend_allocates_fresh_leaf_from_unchanged_params(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    first = automatic_scratch(backend, work)
    await backend.stop()
    other, _ = make_backend(tmp_path, monkeypatch, work)
    second = automatic_scratch(other, work)
    assert first != second and not first.exists() and second.is_dir()
    await other.stop()


@pytest.mark.asyncio
async def test_already_removed_leaf_is_harmless(tmp_path, monkeypatch):
    backend, work = make_backend(tmp_path, monkeypatch)
    scratch = automatic_scratch(backend, work)
    scratch.rmdir()
    await backend.stop()
    assert not backend._windows_scratch


@pytest.mark.asyncio
async def test_scratch_never_exported_before_stop_ordinary_edit_survives(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    task = project / "TASK.md"
    task.write_text("Task")
    code = project / "code.txt"
    code.write_text("before")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"], cwd=project, check=True)
    snapshot = create_workspace_snapshot(project, task)
    backend, work = make_backend(tmp_path, monkeypatch, snapshot.snapshot_root)
    try:
        scratch = automatic_scratch(backend, work)
        (scratch / "ordinary-no-cache-suffix.txt").write_text("temporary data")
        (work / "code.txt").write_text("after")
        result = apply_snapshot_patch(snapshot)
        assert result.changed_paths == ("code.txt",)
        assert code.read_text() == "after" and not (project / "__pycache__").exists()
        assert scratch.is_dir() and not backend._closing
        # Snapshot disposal may precede backend.stop during finalization.
        snapshot.cleanup()
        await backend.stop()
    finally:
        if not backend._closing:
            await backend.stop()
        snapshot.cleanup()
