"""Durable, controller-private authority for live workspace recovery.

The caller owns the controller lifetime lock and fences previous workers before
restoring. Files here must live under the original project's protected
``.supervisor`` directory, never in the model's writable snapshot. A digest in a
model-written JSON file is not authority. Detached recovery exports deliberately
cannot be loaded by this format.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from supervisor.filesystem_safety import is_link_or_reparse

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import WorkspaceSnapshot, SnapshotPatchResult


class _StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _PathState(_StrictRecord):
    kind: Literal["absent", "directory", "file", "symlink"]
    sha256: str | None = None
    executable: bool = False
    symlink_target: str | None = None


class _ManifestEntry(_StrictRecord):
    path: str
    state: _PathState


class _Rewrite(_StrictRecord):
    path: str
    original_target: str
    snapshot_target: str


class _Authority(_StrictRecord):
    version: Literal[1]
    run_id: str
    original_root: str
    original_root_identity: list[int] = Field(min_length=2, max_length=2)
    snapshot_root: str
    snapshot_root_identity: list[int] = Field(min_length=2, max_length=2)
    temp_root: str
    temp_root_identity: list[int] = Field(min_length=2, max_length=2)
    git_root_identity: list[int] = Field(min_length=2, max_length=2)
    state_root_identity: list[int] = Field(min_length=2, max_length=2)
    authority_parent_identity: list[int] = Field(min_length=2, max_length=2)
    task_relative_path: str
    task_bytes: str
    task_sha256: str
    baseline_commit: str
    baseline_objects: dict[str, str]
    baseline_tree_sha256: str
    git_config_bytes: str
    git_config_mode: int
    git_worktree_config_bytes: str | None
    git_worktree_config_mode: int | None
    plan_relative_path: str | None
    plan_bytes: str | None
    plan_sha256: str | None
    plan_exposed: bool
    readonly_dependency_paths: list[str]
    readonly_dependency_roots: list[str]
    dependency_identities: dict[str, list[int]]
    declared_grading_roots: list[str]
    rewritten_symlinks: list[_Rewrite]
    excluded_external_symlink_paths: list[str]
    runtime_exposure_mode: Literal["copy", "symlink"]
    runtime_copy_manifests: dict[str, list[_ManifestEntry]]
    runtime_integrity_issues: list[str]


class _PatchResult(_StrictRecord):
    applied: bool
    changed_paths: list[str]
    patch_bytes: int = Field(ge=0)
    ignored_paths: list[str]


class _PatchTransaction(_StrictRecord):
    version: Literal[1]
    run_id: str
    authority_digest: str
    disposition: Literal["applying", "committed"]
    result: _PatchResult | None
    paths: dict[str, _PathState]


def _error(message: str) -> Exception:
    from supervisor.workspace_snapshot import WorkspaceSnapshotError

    return WorkspaceSnapshotError(f"snapshot recovery rejected: {message}")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def _load_record(data: bytes, model: type[_StrictRecord]) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate authority key")
            result[key] = value
        return result

    raw = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(raw, dict) or type(raw.get("version")) is not int:
        raise ValueError("authority version must be an integer")
    return model.model_validate(raw)


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path != path.absolute()
        or str(path) != str(value)
    ):
        raise _error("authority contains a noncanonical absolute path")
    return path


def _regular_chain(path: Path, *, directory: bool) -> os.stat_result:
    """Reject redirected ancestors before opening any protected object."""
    path = _absolute(path)
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if is_link_or_reparse(parent, stat_result=metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise _error(f"path ancestor is linked or is not a directory: {parent}")
    metadata = path.lstat()
    if is_link_or_reparse(path, stat_result=metadata):
        raise _error(f"path is linked or redirected: {path}")
    if directory:
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_ino == 0:
            raise _error(f"directory has no trustworthy identity: {path}")
    elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _error(f"file is not a single regular entry: {path}")
    return metadata


def _identity(path: Path) -> list[int]:
    metadata = _regular_chain(path, directory=True)
    return [metadata.st_dev, metadata.st_ino]


def _relative(value: str, *, allow_dot: bool = False) -> str:
    if allow_dot and value == ".":
        return value
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or path.is_absolute()
        or windows.drive
        or windows.is_absolute()
        or "\\" in value
        or "\0" in value
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise _error("authority contains an unsafe relative path")
    return value


def _protected_path(path: Path, project_root: Path, *, create: bool = False) -> Path:
    root = _absolute(project_root)
    _regular_chain(root, directory=True)
    path = _absolute(path)
    try:
        relative = path.relative_to(root / ".supervisor")
    except ValueError as exc:
        raise _error(
            "authority must be stored in the original protected .supervisor directory"
        ) from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise _error("authority filename is invalid")
    current = root
    for part in (".supervisor", *relative.parts[:-1]):
        current /= part
        if create and not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        _regular_chain(current, directory=True)
    if path.exists() or path.is_symlink():
        metadata = _regular_chain(path, directory=False)
        if os.name != "nt" and (
            metadata.st_uid != os.getuid() or metadata.st_mode & 0o022
        ):
            raise _error("authority file owner or write permissions changed")
    return path


def _read_private(path: Path, project_root: Path) -> bytes:
    from supervisor import workspace_snapshot as ops

    path = _protected_path(path, project_root)
    before = _regular_chain(path, directory=False)
    data, _mode = ops._read_regular_file(path)
    after = _regular_chain(path, directory=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise _error("authority changed while reading")
    return data


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # Windows replacement is atomic; portable directory fsync is unavailable.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, project_root: Path, data: bytes) -> None:
    path = _protected_path(path, project_root, create=True)
    if path.exists() and _read_private(path, project_root) == data:
        return
    parent_identity = _identity(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _identity(path.parent) != parent_identity:
            raise _error("authority parent changed during write")
        _protected_path(path, project_root)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _error("authority contains malformed encoded bytes") from exc


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _validate_git_tree(snapshot: WorkspaceSnapshot) -> None:
    """No Git subprocess may encounter linked metadata or object redirection."""
    root = snapshot.snapshot_root / ".git"
    _regular_chain(root, directory=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        _regular_chain(parent, directory=True)
        for name in dirs:
            _regular_chain(parent / name, directory=True)
        for name in files:
            _regular_chain(parent / name, directory=False)
    for name in (
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "shallow",
    ):
        if (root / name).exists():
            raise _error("Git object redirection or shallow metadata is unsupported")
    replace = root / "refs" / "replace"
    if replace.exists() and any(replace.iterdir()):
        raise _error("Git replacement objects are unsupported")
    if not snapshot.git_control_is_trusted():
        raise _error("saved Git control bytes no longer match")
    for name, mode in (
        ("config", snapshot.git_config_mode),
        ("config.worktree", snapshot.git_worktree_config_mode),
    ):
        if mode is not None and stat.S_IMODE((root / name).lstat().st_mode) != mode:
            raise _error("saved Git control mode no longer matches")


def _hash_git_objects(
    snapshot: WorkspaceSnapshot, objects: dict[str, str]
) -> dict[str, str]:
    """Hash immutable objects using one Git reader, without loading a repo in RAM."""
    from supervisor import workspace_snapshot as ops

    env = ops._isolated_git_env()
    env["GIT_NO_LAZY_FETCH"] = "1"
    process = subprocess.Popen(
        [
            ops._git_executable(snapshot.snapshot_root),
            "--no-replace-objects",
            "cat-file",
            "--batch",
        ],
        cwd=snapshot.snapshot_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    hashes: dict[str, str] = {}
    try:
        assert process.stdin is not None and process.stdout is not None
        for object_id, kind in objects.items():
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = (
                process.stdout.readline(256).decode("ascii").rstrip("\n").split(" ")
            )
            if len(header) != 3 or header[:2] != [object_id, kind]:
                raise _error("a baseline Git object is missing or has changed type")
            size = int(header[2])
            if size < 0:
                raise _error("a baseline Git object has an invalid size")
            digest = hashlib.sha256()
            while size:
                chunk = process.stdout.read(min(size, 1024 * 1024))
                if not chunk:
                    raise _error("a baseline Git object is truncated")
                size -= len(chunk)
                digest.update(chunk)
            if process.stdout.read(1) != b"\n":
                raise _error("a baseline Git object has invalid framing")
            hashes[object_id] = digest.hexdigest()
        process.stdin.close()
        if process.wait(timeout=30) != 0:
            raise _error("baseline Git object reader failed")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
    return hashes


def _baseline_authority(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    from supervisor import workspace_snapshot as ops

    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", snapshot.baseline_commit):
        raise _error("invalid baseline commit")
    _validate_git_tree(snapshot)
    raw = ops._run_git(
        snapshot.snapshot_root,
        ["--no-replace-objects", "ls-tree", "-r", "-z", snapshot.baseline_commit],
        capture_bytes=True,
    )
    assert isinstance(raw, bytes)
    objects = {snapshot.baseline_commit: "commit"}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        _relative(name.decode("utf-8", errors="surrogateescape"))
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise _error("baseline contains unsupported Git entries")
        objects[object_id] = kind
    return {
        "baseline_objects": _hash_git_objects(snapshot, objects),
        "baseline_tree_sha256": _digest(raw),
    }


def _validate_saved_directory_identities(
    snapshot: WorkspaceSnapshot, authority_path: Path
) -> None:
    for path, key in (
        (snapshot.original_root, "original_root_identity"),
        (snapshot.snapshot_root, "snapshot_root_identity"),
        (snapshot.temp_root, "temp_root_identity"),
        (snapshot.snapshot_root / ".git", "git_root_identity"),
        (snapshot.original_root / ".supervisor", "state_root_identity"),
        (authority_path.parent, "authority_parent_identity"),
    ):
        expected = (
            list(snapshot.original_root_identity)
            if key == "original_root_identity"
            else snapshot.recovery_identity[key]
        )
        if _identity(path) != expected:
            raise _error("snapshot or original directory identity changed")


_IMMUTABLE_AUTHORITY_FIELDS = (
    "original_root",
    "original_root_identity",
    "snapshot_root",
    "temp_root",
    "task_relative_path",
    "task_bytes",
    "task_sha256",
    "baseline_commit",
    "git_config_bytes",
    "git_config_mode",
    "git_worktree_config_bytes",
    "git_worktree_config_mode",
    "plan_relative_path",
    "plan_bytes",
    "plan_sha256",
    "plan_exposed",
    "readonly_dependency_paths",
    "readonly_dependency_roots",
    "declared_grading_roots",
    "rewritten_symlinks",
    "excluded_external_symlink_paths",
    "runtime_exposure_mode",
)


def _authority_signature(snapshot: WorkspaceSnapshot) -> tuple[Any, ...]:
    # These are frozen values. Runtime manifests are immutable tuples of frozen
    # path states; the runtime service replaces a tuple when authority changes.
    return (
        *(getattr(snapshot, name) for name in _IMMUTABLE_AUTHORITY_FIELDS),
        tuple(sorted(snapshot.runtime_copy_manifests.items())),
        tuple(snapshot.runtime_integrity_issues),
    )


def _authority_file_stamp(path: Path) -> tuple[int, ...]:
    metadata = _regular_chain(path, directory=False)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture(
    snapshot: WorkspaceSnapshot, run_id: str, authority_path: Path
) -> dict[str, Any]:
    if str(UUID(run_id)) != run_id:
        raise _error("run ID is not a canonical UUID")
    if not snapshot.recovery_identity:
        identity = {
            "snapshot_root_identity": _identity(snapshot.snapshot_root),
            "temp_root_identity": _identity(snapshot.temp_root),
            "git_root_identity": _identity(snapshot.snapshot_root / ".git"),
            "state_root_identity": _identity(snapshot.original_root / ".supervisor"),
            "authority_parent_identity": _identity(authority_path.parent),
            "dependency_identities": {
                str(
                    (snapshot.original_root / relative).resolve(strict=True)
                ): _identity((snapshot.original_root / relative).resolve(strict=True))
                for relative in snapshot.readonly_dependency_paths
            },
            **_baseline_authority(snapshot),
        }
        snapshot.recovery_identity.update(identity)
    _validate_saved_directory_identities(snapshot, authority_path)
    identity = snapshot.recovery_identity
    record = {
        "version": 1,
        "run_id": run_id,
        "original_root": str(snapshot.original_root),
        "original_root_identity": list(snapshot.original_root_identity),
        "snapshot_root": str(snapshot.snapshot_root),
        "temp_root": str(snapshot.temp_root),
        "task_relative_path": snapshot.task_relative_path,
        "task_bytes": _encode(snapshot.task_bytes),
        "task_sha256": snapshot.task_sha256,
        "baseline_commit": snapshot.baseline_commit,
        "git_config_bytes": _encode(snapshot.git_config_bytes),
        "git_config_mode": snapshot.git_config_mode,
        "git_worktree_config_bytes": None
        if snapshot.git_worktree_config_bytes is None
        else _encode(snapshot.git_worktree_config_bytes),
        "git_worktree_config_mode": snapshot.git_worktree_config_mode,
        "plan_relative_path": snapshot.plan_relative_path,
        "plan_bytes": None
        if snapshot.plan_bytes is None
        else _encode(snapshot.plan_bytes),
        "plan_sha256": snapshot.plan_sha256,
        "plan_exposed": snapshot.plan_exposed,
        "readonly_dependency_paths": list(snapshot.readonly_dependency_paths),
        "readonly_dependency_roots": [
            str(root) for root in snapshot.readonly_dependency_roots
        ],
        "declared_grading_roots": [
            str(root) for root in snapshot.declared_grading_roots
        ],
        "rewritten_symlinks": [asdict(item) for item in snapshot.rewritten_symlinks],
        "excluded_external_symlink_paths": list(
            snapshot.excluded_external_symlink_paths
        ),
        "runtime_exposure_mode": snapshot.runtime_exposure_mode,
        "runtime_copy_manifests": {
            label: [{"path": name, "state": asdict(state)} for name, state in entries]
            for label, entries in snapshot.runtime_copy_manifests.items()
        },
        "runtime_integrity_issues": list(snapshot.runtime_integrity_issues),
        **identity,
    }
    return _Authority.model_validate(record).model_dump()


def persist_snapshot_authority(
    snapshot: WorkspaceSnapshot, authority_path: Path, *, run_id: str
) -> str:
    """Publish trusted in-memory authority while holding controller ownership."""
    try:
        if snapshot.recovery_run_id not in (None, run_id):
            raise _error("a snapshot cannot be transferred between run identities")
        path = _protected_path(authority_path, snapshot.original_root, create=True)
        signature = _authority_signature(snapshot)
        cache = snapshot.recovery_cache
        if snapshot.recovery_identity:
            _validate_saved_directory_identities(snapshot, path)
        if (
            snapshot.recovery_authority_digest is not None
            and cache.get("signature") == signature
            and cache.get("path") == path
            and cache.get("stamp") == _authority_file_stamp(path)
        ):
            # This never adopts disk contents or a digest from metadata. Even
            # adversarial same-size/timestamp rewrites stay bound to the old
            # digest and fail the mandatory full hash on restore/final apply.
            return snapshot.recovery_authority_digest
        data = _json_bytes(_capture(snapshot, run_id, path))
        if snapshot.recovery_authority_digest is not None:
            previous = _read_private(path, snapshot.original_root)
            if _digest(previous) != snapshot.recovery_authority_digest:
                raise _error("existing authority was modified outside the controller")
        _write_private(path, snapshot.original_root, data)
        digest = _digest(data)
        object.__setattr__(snapshot, "recovery_authority_path", path)
        object.__setattr__(snapshot, "recovery_run_id", run_id)
        object.__setattr__(snapshot, "recovery_authority_digest", digest)
        cache.update(signature=signature, path=path, stamp=_authority_file_stamp(path))
        return digest
    except (OSError, ValueError, TypeError, ValidationError):
        # Schema errors can embed the whole input, including the private plan.
        raise _error("cannot persist live snapshot authority") from None


def _restore_record(
    record: _Authority, project_root: Path, authority_path: Path
) -> WorkspaceSnapshot:
    from supervisor import workspace_snapshot as ops

    original = _absolute(record.original_root)
    snapshot_root = _absolute(record.snapshot_root)
    temp_root = _absolute(record.temp_root)
    if original != project_root or snapshot_root != temp_root / "workspace":
        raise _error("snapshot location does not match the saved project topology")
    if original.is_relative_to(temp_root) or temp_root.is_relative_to(original):
        raise _error("snapshot temporary tree overlaps the original workspace")
    for path, expected in (
        (original, record.original_root_identity),
        (snapshot_root, record.snapshot_root_identity),
        (temp_root, record.temp_root_identity),
        (snapshot_root / ".git", record.git_root_identity),
        (original / ".supervisor", record.state_root_identity),
        (authority_path.parent, record.authority_parent_identity),
    ):
        if _identity(path) != expected:
            raise _error("saved directory identity changed")
    task_relative = _relative(record.task_relative_path)
    for value in [
        task_relative,
        *record.readonly_dependency_paths,
        *record.excluded_external_symlink_paths,
        *(item.path for item in record.rewritten_symlinks),
    ]:
        _relative(value)
    task_bytes = _decode(record.task_bytes)
    if _digest(task_bytes) != record.task_sha256:
        raise _error("saved task bytes do not match their identity")
    task_source = original / task_relative
    _regular_chain(task_source, directory=False)
    if not ops._regular_file_matches(task_source, task_bytes):
        raise _error("original task changed")
    plan_relative = (
        None
        if record.plan_relative_path is None
        else _relative(record.plan_relative_path)
    )
    plan_bytes = None if record.plan_bytes is None else _decode(record.plan_bytes)
    if plan_relative is None:
        if (
            plan_bytes is not None
            or record.plan_sha256 is not None
            or record.plan_exposed
        ):
            raise _error("incomplete private plan authority")
    else:
        if plan_bytes is None or _digest(plan_bytes) != record.plan_sha256:
            raise _error("private plan bytes do not match their identity")
        _regular_chain(original / plan_relative, directory=False)
        if not ops._regular_file_matches(original / plan_relative, plan_bytes):
            raise _error("original private plan changed")
    dependency_roots = tuple(
        _absolute(value) for value in record.readonly_dependency_roots
    )
    expected_dependency_sources = {
        str((original / relative).resolve(strict=True))
        for relative in record.readonly_dependency_paths
    }
    if set(record.dependency_identities) != expected_dependency_sources:
        raise _error("dependency root mapping changed")
    for raw, identity in record.dependency_identities.items():
        if _identity(_absolute(raw)) != identity:
            raise _error("dependency root identity changed")
    if (
        record.runtime_exposure_mode == "symlink"
        and set(map(str, dependency_roots)) != expected_dependency_sources
    ):
        raise _error("dependency sandbox scope does not match captured roots")
    if record.runtime_exposure_mode != ops._runtime_exposure_mode():
        raise _error("saved runtime exposure mode is incompatible with this host")
    manifests = {}
    allowed_labels = {
        "task",
        "plan",
        "supervisor_state",
        *(f"dependency:{relative}" for relative in record.readonly_dependency_paths),
    }
    for label, entries in record.runtime_copy_manifests.items():
        if label not in allowed_labels:
            raise _error("unknown runtime authority label")
        manifests[label] = tuple(
            (
                _relative(entry.path, allow_dot=True),
                ops.SnapshotPathState(**entry.state.model_dump()),
            )
            for entry in entries
        )
    if record.runtime_integrity_issues:
        raise _error("saved snapshot already has runtime integrity failures")
    snapshot = ops.WorkspaceSnapshot(
        original_root=original,
        original_root_identity=tuple(record.original_root_identity),
        snapshot_root=snapshot_root,
        temp_root=temp_root,
        task_path=snapshot_root / task_relative,
        task_relative_path=task_relative,
        task_bytes=task_bytes,
        task_sha256=record.task_sha256,
        baseline_commit=record.baseline_commit,
        git_config_bytes=_decode(record.git_config_bytes),
        git_config_mode=record.git_config_mode,
        git_worktree_config_bytes=None
        if record.git_worktree_config_bytes is None
        else _decode(record.git_worktree_config_bytes),
        git_worktree_config_mode=record.git_worktree_config_mode,
        plan_source_path=None if plan_relative is None else original / plan_relative,
        plan_path=None if plan_relative is None else snapshot_root / plan_relative,
        plan_relative_path=plan_relative,
        plan_bytes=plan_bytes,
        plan_sha256=record.plan_sha256,
        plan_exposed=record.plan_exposed,
        readonly_dependency_paths=tuple(record.readonly_dependency_paths),
        readonly_dependency_roots=dependency_roots,
        declared_grading_roots=tuple(record.declared_grading_roots),
        rewritten_symlinks=tuple(
            ops.SnapshotSymlinkRewrite(**item.model_dump())
            for item in record.rewritten_symlinks
        ),
        excluded_external_symlink_paths=tuple(record.excluded_external_symlink_paths),
        runtime_exposure_mode=record.runtime_exposure_mode,
        runtime_copy_manifests=manifests,
        recovery_identity={
            key: getattr(record, key)
            for key in (
                "snapshot_root_identity",
                "temp_root_identity",
                "git_root_identity",
                "state_root_identity",
                "authority_parent_identity",
                "dependency_identities",
                "baseline_objects",
                "baseline_tree_sha256",
            )
        },
    )
    if _baseline_authority(snapshot) != {
        "baseline_objects": record.baseline_objects,
        "baseline_tree_sha256": record.baseline_tree_sha256,
    }:
        raise _error("baseline Git content changed")
    _validate_runtime(snapshot)
    try:
        if ops._native_windows_runtime_controls_enabled():
            snapshot.windows_runtime_file_guards["task"] = (
                ops._WindowsRuntimeFileGuard.open(snapshot.task_path)
            )
            if snapshot.plan_exposed and snapshot.plan_path is not None:
                snapshot.windows_runtime_file_guards["plan"] = (
                    ops._WindowsRuntimeFileGuard.open(snapshot.plan_path)
                )
            for relative in snapshot.readonly_dependency_paths:
                snapshot.windows_dependency_watchers[f"dependency:{relative}"] = (
                    ops._WindowsDirectoryChangeWatcher.open(
                        snapshot.snapshot_root / relative
                    )
                )
            _validate_runtime(snapshot)
        return snapshot
    except BaseException:
        ops._close_windows_runtime_controls(
            snapshot.windows_runtime_file_guards, snapshot.windows_dependency_watchers
        )
        raise


def _validate_runtime(snapshot: WorkspaceSnapshot) -> None:
    from supervisor import workspace_snapshot as ops

    _regular_chain(snapshot.task_path.parent, directory=True)
    if snapshot.plan_path is not None and snapshot.plan_exposed:
        _regular_chain(snapshot.plan_path.parent, directory=True)
    if issue := snapshot.task_integrity_issue():
        raise _error(issue)
    if issue := snapshot.plan_integrity_issue():
        raise _error(issue)
    mounts = {
        "supervisor_state": (
            snapshot.snapshot_root / ".supervisor",
            snapshot.original_root / ".supervisor",
        )
    }
    mounts.update(
        {
            f"dependency:{relative}": (
                snapshot.snapshot_root / relative,
                snapshot.original_root / relative,
            )
            for relative in snapshot.readonly_dependency_paths
        }
    )
    for label, (destination, source) in mounts.items():
        if snapshot.runtime_exposure_mode == "symlink":
            if not ops._symlink_points_to(destination, source):
                raise _error(f"runtime mount was replaced: {label}")
            _regular_chain(destination.parent, directory=True)
        elif ops._runtime_exposure_manifest(
            destination
        ) != snapshot.runtime_copy_manifests.get(label, ()):
            raise _error(f"runtime copy changed: {label}")
    if (
        not snapshot.plan_exposed
        and snapshot.plan_path is not None
        and (snapshot.plan_path.exists() or snapshot.plan_path.is_symlink())
    ):
        raise _error("private plan exposure reappeared after detachment")


def restore_snapshot_authority(
    authority_path: Path, *, run_id: str, expected_digest: str, project_root: Path
) -> WorkspaceSnapshot:
    """Validate without repairs, then reopen native guards on the same live tree."""
    try:
        project_root = _absolute(project_root)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise _error("authority digest is malformed")
        data = _read_private(authority_path, project_root)
        if _digest(data) != expected_digest:
            raise _error("protected authority digest changed")
        record = _load_record(data, _Authority)
        if record.run_id != run_id or str(UUID(run_id)) != run_id:
            raise _error("authority belongs to another run")
        snapshot = _restore_record(record, project_root, authority_path)
        object.__setattr__(snapshot, "recovery_authority_path", authority_path)
        object.__setattr__(snapshot, "recovery_run_id", run_id)
        object.__setattr__(snapshot, "recovery_authority_digest", expected_digest)
        return snapshot
    except (OSError, ValueError, TypeError, ValidationError):
        raise _error("live snapshot authority is unavailable or invalid") from None


def _transaction_path(snapshot: WorkspaceSnapshot) -> Path | None:
    path = snapshot.recovery_authority_path
    return None if path is None else path.with_name("snapshot-apply.json")


def _original_output_state(snapshot: WorkspaceSnapshot, raw: str) -> dict[str, Any]:
    from supervisor import workspace_snapshot as ops

    relative = _relative(raw)
    parent = snapshot.original_root
    for component in PurePosixPath(relative).parts[:-1]:
        parent /= component
        try:
            _regular_chain(parent, directory=True)
        except FileNotFoundError:
            return asdict(ops.SnapshotPathState(kind="absent"))
    return asdict(ops._snapshot_path_state(snapshot.original_root / relative))


def previous_patch_result(snapshot: WorkspaceSnapshot) -> SnapshotPatchResult | None:
    """Never replay a transaction with an uncertain or changed outcome."""
    from supervisor import workspace_snapshot as ops

    path = _transaction_path(snapshot)
    if path is None:
        return None
    if (
        snapshot.recovery_authority_path is None
        or snapshot.recovery_authority_digest is None
    ):
        raise _error("patch authority binding is incomplete")
    if (
        _digest(_read_private(snapshot.recovery_authority_path, snapshot.original_root))
        != snapshot.recovery_authority_digest
    ):
        raise _error("patch authority was modified outside the controller")
    if _identity(snapshot.original_root) != list(snapshot.original_root_identity):
        raise _error("original workspace identity changed before patch application")
    _protected_path(path, snapshot.original_root)
    if not path.exists():
        return None
    try:
        record = _load_record(
            _read_private(path, snapshot.original_root), _PatchTransaction
        )
        if (
            record.run_id != snapshot.recovery_run_id
            or record.authority_digest != snapshot.recovery_authority_digest
        ):
            raise _error("patch transaction metadata is invalid")
        if record.disposition != "committed":
            raise _error(
                "prior patch application has an uncertain outcome; manual inspection is required"
            )
        result = record.result
        if result is None:
            raise _error("committed patch result is malformed")
        paths = record.paths
        if set(paths) != set(result.changed_paths):
            raise _error("committed patch manifest is malformed")
        for raw, expected in paths.items():
            if _original_output_state(snapshot, raw) != expected.model_dump():
                raise _error("committed patch output changed; refusing to reapply it")
        return ops.SnapshotPatchResult(
            applied=result.applied,
            changed_paths=tuple(result.changed_paths),
            patch_bytes=result.patch_bytes,
            ignored_paths=tuple(result.ignored_paths),
        )
    except (OSError, ValueError, TypeError, KeyError):
        raise _error(
            "prior patch transaction is unreadable; refusing to reapply"
        ) from None


def begin_patch_transaction(snapshot: WorkspaceSnapshot) -> None:
    path = _transaction_path(snapshot)
    if path is None:
        return
    if previous_patch_result(snapshot) is not None:
        raise _error("patch application was already committed")
    record = {
        "version": 1,
        "run_id": snapshot.recovery_run_id,
        "authority_digest": snapshot.recovery_authority_digest,
        "disposition": "applying",
        "result": None,
        "paths": {},
    }
    _write_private(path, snapshot.original_root, _json_bytes(record))


def commit_patch_transaction(
    snapshot: WorkspaceSnapshot, result: SnapshotPatchResult
) -> None:
    from supervisor import workspace_snapshot as ops

    path = _transaction_path(snapshot)
    if path is None:
        return
    paths = {}
    for raw in result.changed_paths:
        expected = asdict(ops._snapshot_path_state(snapshot.snapshot_root / raw))
        if _original_output_state(snapshot, raw) != expected:
            raise _error(
                "original output changed before the patch commit became durable"
            )
        paths[raw] = expected
    record = {
        "version": 1,
        "run_id": snapshot.recovery_run_id,
        "authority_digest": snapshot.recovery_authority_digest,
        "disposition": "committed",
        "result": asdict(result),
        "paths": paths,
    }
    _write_private(path, snapshot.original_root, _json_bytes(record))
