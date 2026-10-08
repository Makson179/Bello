"""Snapshot manifests, candidate selection, and content comparison.

Comparison never repairs files or changes snapshot-owned baselines. Candidate
selection stages only the disposable snapshot index, with runtime paths excluded."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        VerificationWorkspaceSnapshot,
        SnapshotPatchSelection,
        SnapshotPathState,
    )


def _snapshot_patch_selection(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> SnapshotPatchSelection:
    snapshot_root = snapshot.snapshot_root
    excluded = [
        ".supervisor",
        *(
            (snapshot.plan_relative_path,)
            if snapshot.plan_relative_path is not None
            else ()
        ),
        *snapshot.readonly_dependency_paths,
    ]
    pathspecs = [".", *(f":(exclude,top,literal){path}" for path in excluded)]
    # Exclude controller-owned runtime copies before Git walks the tree.  The
    # previous add-then-filter flow unnecessarily exposed large dependency
    # trees (and any transient corruption in them) to an unsandboxed Git.
    ops._run_git(snapshot_root, ["add", "-f", "-A", "--", *pathspecs])
    raw = ops._run_git(
        snapshot_root,
        [
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--name-only",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            snapshot.baseline_commit,
            "--",
        ],
        capture_bytes=True,
    )
    assert isinstance(raw, bytes)
    changed_paths = tuple(
        part.decode("utf-8", errors="surrogateescape")
        for part in raw.split(b"\0")
        if part
    )
    return ops._filter_snapshot_patch_paths(
        snapshot_root,
        changed_paths,
        readonly_dependency_paths=snapshot.readonly_dependency_paths,
        private_runtime_paths=(
            (snapshot.plan_relative_path,)
            if snapshot.plan_relative_path is not None
            else ()
        ),
    )


def _filter_snapshot_patch_paths(
    ops: SnapshotServices,
    /,
    snapshot_root: Path,
    changed_paths: tuple[str, ...],
    *,
    readonly_dependency_paths: tuple[str, ...],
    private_runtime_paths: tuple[str, ...] = (),
) -> SnapshotPatchSelection:
    kept: list[str] = []
    ignored: list[str] = []
    for path in changed_paths:
        if (
            ops._is_generated_artifact_path(snapshot_root, path)
            or any(
                ops._path_is_at_or_below(path, dependency)
                for dependency in readonly_dependency_paths
            )
            or any(
                ops._path_is_at_or_below(path, private_path)
                for private_path in private_runtime_paths
            )
        ):
            ignored.append(path)
        else:
            kept.append(path)
    return ops.SnapshotPatchSelection(tuple(kept), tuple(ignored))


def _snapshot_patch(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot, changed_paths: Sequence[str]
) -> bytes:
    raw = ops._run_git(
        snapshot.snapshot_root,
        [
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            snapshot.baseline_commit,
            "--",
            *changed_paths,
        ],
        capture_bytes=True,
    )
    assert isinstance(raw, bytes)
    return raw


def _verification_git_manifest(
    ops: SnapshotServices, /, snapshot_root: Path
) -> tuple[tuple[str, str], ...]:
    """Capture review-relevant Git semantics without hashing mutable object storage.

    Commands run during review may legitimately populate object/cache files, but they must
    not change which submitted revision/index/config the reviewer is judging.
    """

    entries: list[tuple[str, str]] = []
    for label, args in (
        ("head", ["rev-parse", "--verify", "-q", "HEAD"]),
        ("symbolic_head", ["symbolic-ref", "-q", "HEAD"]),
        ("refs", ["for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"]),
        ("index", ["ls-files", "--stage", "-v", "-z"]),
        ("safe_config", ["config", "--local", "--list", "--null"]),
    ):
        completed = subprocess.run(
            [ops._git_executable(snapshot_root), *args],
            cwd=snapshot_root,
            env=ops._isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if label in {"head", "symbolic_head"} and completed.returncode in {1, 128}:
            value = ""
        elif completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ops.WorkspaceSnapshotError(
                f"failed to capture verification Git {label}: {detail or completed.returncode}"
            )
        else:
            value = hashlib.sha256(completed.stdout).hexdigest()
        entries.append((label, value))
    for relative in ("info/exclude", "info/sparse-checkout"):
        path = ops._git_metadata_path(snapshot_root, relative, required=False)
        entries.append((relative, ops._sha256_file(path) if path is not None else ""))
    return tuple(entries)


def _verification_git_control_manifest(
    ops: SnapshotServices, /, snapshot_root: Path
) -> tuple[tuple[str, SnapshotPathState], ...]:
    git_dir = snapshot_root / ".git"
    entries: list[tuple[str, SnapshotPathState]] = []
    for current, dirs, files in os.walk(git_dir, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(git_dir)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if relative == "logs" or relative.startswith("logs/"):
                continue
            if ops.is_link_or_reparse(path):
                entries.append((relative, ops._snapshot_path_state(path)))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if relative in {
                "HEAD",
                "config",
                "config.worktree",
                "index",
                "index.lock",
                "ORIG_HEAD",
                "FETCH_HEAD",
                "COMMIT_EDITMSG",
                "info/exclude",
                "info/sparse-checkout",
            }:
                continue
            state = ops._snapshot_path_state(path)
            if state.kind in {"file", "symlink", "reparse"}:
                entries.append((relative, state))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _verification_worktree_manifest(
    ops: SnapshotServices, /, snapshot_root: Path
) -> tuple[tuple[str, SnapshotPathState], ...]:
    entries: list[tuple[str, SnapshotPathState]] = []
    for current, dirs, files in os.walk(snapshot_root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(snapshot_root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if ops._name_key(name) == ops._name_key(".git"):
                continue
            if ops.is_link_or_reparse(path):
                entries.append((relative, ops._snapshot_path_state(path)))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            relative = (relative_dir / name).as_posix()
            path = current_path / name
            state = ops._snapshot_path_state(path)
            if state.kind in {"file", "symlink", "reparse"}:
                entries.append((relative, state))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _is_verification_mutable_artifact_path(
    ops: SnapshotServices, /, raw_path: str
) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in ops.VERIFICATION_MUTABLE_ARTIFACT_DIR_NAMES for part in parts):
        return True
    lowered_name = relative.name.lower()
    if lowered_name in ops.GENERATED_ARTIFACT_FILE_NAMES:
        return True
    return any(
        lowered_name.endswith(suffix) for suffix in ops.GENERATED_ARTIFACT_SUFFIXES
    )


def _verification_path_is_git_ignored(
    ops: SnapshotServices, /, snapshot_root: Path, raw_path: str
) -> bool:
    completed = subprocess.run(
        [ops._git_executable(snapshot_root), "check-ignore", "-q", "--", raw_path],
        cwd=snapshot_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _verification_mutable_submitted_paths(
    ops: SnapshotServices,
    /,
    snapshot_root: Path,
    manifest: tuple[tuple[str, SnapshotPathState], ...],
) -> tuple[str, ...]:
    tracked: set[str] = set()
    if ops._is_top_level_git_repository(snapshot_root):
        raw = ops._run_git(snapshot_root, ["ls-files", "-z"], capture_bytes=True)
        assert isinstance(raw, bytes)
        tracked = {
            part.decode("utf-8", errors="surrogateescape")
            for part in raw.split(b"\0")
            if part
        }
    mutable: list[str] = []
    for raw_path, state in manifest:
        if raw_path in tracked or state.kind != "file":
            continue
        if ops._verification_path_is_git_ignored(
            snapshot_root, raw_path
        ) or ops._looks_like_build_artifact(
            snapshot_root / raw_path,
            raw_path,
        ):
            mutable.append(raw_path)
    return tuple(sorted(mutable))


def _looks_like_build_artifact(
    ops: SnapshotServices, /, path: Path, raw_path: str
) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in ops.VERIFICATION_BUILD_ARTIFACT_DIR_NAMES for part in parts):
        return True
    lowered_name = relative.name.lower()
    if lowered_name in ops.GENERATED_ARTIFACT_FILE_NAMES or any(
        lowered_name.endswith(suffix)
        for suffix in ops.VERIFICATION_BUILD_ARTIFACT_SUFFIXES
    ):
        return True
    try:
        descriptor = ops._open_regular_file_no_follow(path)
    except OSError:
        return False
    try:
        prefix = os.read(descriptor, 8)
    finally:
        os.close(descriptor)
    return (
        prefix.startswith(b"\x7fELF")
        or prefix.startswith(b"MZ")
        or prefix.startswith(b"\x00asm")
        or prefix.startswith(b"!<arch>\n")
        or prefix[:4]
        in {
            b"\xca\xfe\xba\xbe",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
        }
    )


def _runtime_exposure_manifest(
    ops: SnapshotServices, /, path: Path, *, excluded_root_names: tuple[str, ...] = (),
) -> tuple[tuple[str, SnapshotPathState], ...]:
    try:
        root_metadata = path.lstat()
    except FileNotFoundError:
        return ()
    if ops.is_link_or_reparse(path, stat_result=root_metadata):
        raise ops.WorkspaceSnapshotError(
            f"runtime exposure refuses filesystem links or reparse points: {path}"
        )
    root_state = ops._snapshot_path_state(path)
    if root_state.kind == "file":
        ops._assert_stable_regular_entry(path, root_metadata, require_directory=False)
        return ((".", root_state),)
    if root_state.kind != "directory":
        raise ops.WorkspaceSnapshotError(
            f"runtime exposure source contains an unsupported filesystem entry: {path}"
        )

    entries: list[tuple[str, SnapshotPathState]] = [(".", root_state)]
    ops._runtime_directory_manifest(
        path, path, root_metadata, entries, excluded_root_names=excluded_root_names,
    )
    return tuple(sorted(entries, key=lambda item: item[0]))


def _runtime_directory_manifest(
    ops: SnapshotServices,
    /,
    root: Path,
    directory: Path,
    expected: os.stat_result,
    entries: list[tuple[str, SnapshotPathState]],
    *,
    excluded_root_names: tuple[str, ...] = (),
) -> None:
    expected = ops._assert_stable_regular_entry(
        directory, expected, require_directory=True
    )
    excluded = {ops._name_key(name) for name in excluded_root_names}
    children = sorted(
        (child for child in directory.iterdir()
         if directory != root or ops._name_key(child.name) not in excluded),
        key=lambda child: child.name,
    )
    expected = ops._assert_stable_regular_entry(
        directory, expected, require_directory=True
    )
    if ops._is_windows_platform():
        ops._validate_windows_directory_names(
            directory, [child.name for child in children]
        )
    for child in children:
        ops._assert_stable_regular_entry(directory, expected, require_directory=True)
        metadata = child.lstat()
        if ops.is_link_or_reparse(child, stat_result=metadata):
            raise ops.WorkspaceSnapshotError(
                f"runtime exposure refuses filesystem links or reparse points: {child}"
            )
        relative = child.relative_to(root).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, ops.SnapshotPathState(kind="directory")))
            ops._runtime_directory_manifest(root, child, metadata, entries)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ops.WorkspaceSnapshotError(
                f"runtime exposure source contains an unsupported entry: {child}"
            )
        state = ops._snapshot_path_state(child)
        ops._assert_stable_regular_entry(child, metadata, require_directory=False)
        if state.kind != "file":
            raise ops.WorkspaceSnapshotError(
                f"runtime exposure source contains an unsupported file entry: {child}"
            )
        entries.append((relative, state))
    ops._assert_stable_regular_entry(directory, expected, require_directory=True)


def _verify_applied_paths(
    ops: SnapshotServices,
    /,
    original_root: Path,
    snapshot_root: Path,
    changed_paths: tuple[str, ...],
) -> None:
    mismatches: list[str] = []
    for raw in changed_paths:
        expected = ops._snapshot_path_state(snapshot_root / raw)
        actual = ops._snapshot_path_state(original_root / raw)
        if expected != actual:
            mismatches.append(raw)
    if mismatches:
        joined = ", ".join(mismatches[:20])
        raise ops.SnapshotPatchError(
            f"snapshot patch verification failed for: {joined}"
        )


def _snapshot_path_state(ops: SnapshotServices, /, path: Path) -> SnapshotPathState:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ops.SnapshotPathState(kind="absent")
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        return ops.SnapshotPathState(kind="symlink", symlink_target=os.readlink(path))
    if ops.is_reparse_point(path, stat_result=metadata):
        try:
            target = os.readlink(path)
        except OSError:
            target = "<opaque-reparse-point>"
        return ops.SnapshotPathState(kind="reparse", symlink_target=target)
    if stat.S_ISDIR(mode):
        return ops.SnapshotPathState(kind="directory")
    if not stat.S_ISREG(mode):
        return ops.SnapshotPathState(kind="unsupported")
    executable = (
        False
        if ops._is_windows_platform()
        else bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    )
    return ops.SnapshotPathState(
        kind="file", sha256=ops._sha256_file(path), executable=executable
    )


def _is_generated_artifact_path(
    ops: SnapshotServices, /, snapshot_root: Path, raw_path: str
) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in ops.GENERATED_ARTIFACT_DIR_NAMES for part in parts):
        return True
    name = relative.name
    lowered_name = name.lower()
    if lowered_name in ops.GENERATED_ARTIFACT_FILE_NAMES:
        return True
    if any(lowered_name.endswith(suffix) for suffix in ops.GENERATED_ARTIFACT_SUFFIXES):
        return True
    return False


def _assert_submission_unchanged(
    ops: SnapshotServices, /, self: VerificationWorkspaceSnapshot
) -> None:
    before = dict(self.submitted_manifest)
    after = dict(ops._verification_worktree_manifest(self.snapshot_root))
    changed = [
        path
        for path in sorted(before)
        if before.get(path) != after.get(path)
        and path not in self.mutable_submitted_paths
    ]
    # Existing submitted paths are always immutable, even when they live under a
    # conventional build/cache directory. Only newly created, clearly generated paths
    # may remain as incidental outputs of a check.
    changed.extend(
        path
        for path in sorted(after.keys() - before.keys())
        if not ops._is_verification_mutable_artifact_path(path)
        and not ops._verification_path_is_git_ignored(self.snapshot_root, path)
    )
    if self.git_manifest:
        current_git = ops._verification_git_manifest(self.snapshot_root)
        if current_git != self.git_manifest:
            changed.append(".git verification metadata")
    if self.git_control_manifest:
        current_control = ops._verification_git_control_manifest(self.snapshot_root)
        if current_control != self.git_control_manifest:
            changed.append(".git verification control files")
    if not changed:
        return
    detail = ", ".join(changed[:12])
    if len(changed) > 12:
        detail += f", ... (+{len(changed) - 12} more)"
    raise ops.WorkspaceSnapshotError(
        "completion verification modified submitted workspace paths: " + detail
    )
