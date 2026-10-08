"""Self-hosted test artifacts must not weaken completion input completeness."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from supervisor import snapshot_construction
from supervisor.workspace_snapshot import (
    WorkspaceSnapshotError,
    create_verification_workspace_snapshot,
)


def _commit_ignore(source: Path) -> None:
    for args in (
        ["init", "-q"], ["add", ".gitignore"],
        ["-c", "user.name=Snapshot Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)


def test_verification_keeps_ignored_and_untracked_regular_inputs(tmp_path: Path) -> None:
    source = tmp_path / "submitted"
    source.mkdir()
    (source / ".gitignore").write_text(".test-tmp/\n")
    _commit_ignore(source)
    (source / ".test-tmp").mkdir()
    (source / ".test-tmp/input.txt").write_text("relevant submitted evidence")
    (source / "untracked.txt").write_text("untracked implementation")
    subprocess.run(["git", "check-ignore", "-q", ".test-tmp/input.txt"], cwd=source, check=True)
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    assert "?? untracked.txt" in status
    review = create_verification_workspace_snapshot(source)
    try:
        assert (review.snapshot_root / ".test-tmp/input.txt").read_text() == "relevant submitted evidence"
        assert (review.snapshot_root / "untracked.txt").read_text() == "untracked implementation"
    finally:
        review.cleanup()


@pytest.mark.parametrize("kind", ["fifo", "unreadable"])
def test_verification_fails_closed_on_ignored_uncopyable_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, kind: str,
) -> None:
    if os.name != "posix" or (kind == "unreadable" and os.geteuid() == 0):
        pytest.skip("requires non-root POSIX permission/FIFO semantics")
    source = tmp_path / "submitted"
    source.mkdir()
    (source / ".gitignore").write_text(".test-tmp/\n")
    _commit_ignore(source)
    (source / ".test-tmp").mkdir()
    special = source / ".test-tmp/artifact"
    request.addfinalizer(lambda: special.unlink(missing_ok=True))
    if kind == "fifo":
        os.mkfifo(special)
    else:
        special.write_text("unreadable test-only data")
        special.chmod(0o111)
    before = special.lstat()
    allocated = []
    original_mkdtemp = snapshot_construction.tempfile.mkdtemp

    def allocate(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        allocated.append(Path(path))
        return path

    monkeypatch.setattr(snapshot_construction.tempfile, "mkdtemp", allocate)
    with pytest.raises(WorkspaceSnapshotError, match="Keep temporary test artifacts") as caught:
        create_verification_workspace_snapshot(source)
    assert ".test-tmp/artifact" in str(caught.value)
    assert ("named pipe" if kind == "fifo" else "permission denied") in str(caught.value)
    after = special.lstat()
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)
    assert stat.S_ISFIFO(after.st_mode) if kind == "fifo" else stat.S_IMODE(after.st_mode) == 0o111
    assert allocated and all(not path.exists() for path in allocated)


@pytest.mark.parametrize("filename", ["line\n", "\x01" * 160])
def test_verification_copy_diagnostic_is_bounded_and_does_not_emit_raw_errors(tmp_path: Path, filename: str) -> None:
    entries = [(str(tmp_path / f"{filename}{index}"), "/not-reported", "[Errno 13] Permission denied: secret error body") for index in range(100)]
    message = snapshot_construction._verification_copy_failure(shutil.Error(entries), tmp_path)
    assert "+95 more" in message
    assert ("line\\n0" if filename == "line\n" else "\\x01") in message
    assert "\n" not in message
    assert "secret error body" not in message
    assert "/not-reported" not in message
    assert len(message) < 1500


def test_verification_copy_diagnostic_does_not_confuse_filename_with_error(tmp_path: Path) -> None:
    entries = [(str(tmp_path / "named pipe"), "unused", "[Errno 13] Permission denied: 'named pipe'")]
    message = snapshot_construction._verification_copy_failure(shutil.Error(entries), tmp_path)
    assert "(permission denied)" in message
    assert "(named pipe)" not in message
