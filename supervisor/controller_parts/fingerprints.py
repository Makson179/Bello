"""Controller fingerprints; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _large_diff_signature(changed_files: list[compat.ChangedFile]) -> str:
    payload = [
        {
            "path": changed.path,
            "status": changed.status,
            "additions": changed.additions,
            "deletions": changed.deletions,
            "sequence": changed.sequence,
        }
        for changed in sorted(changed_files, key=lambda item: item.path)
    ]
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _restart_budget_signature(health: compat.HealthState, reason: str) -> str:
    payload = {
        "generation": health.generation,
        "reason": reason,
        # A continuing threshold breach is one state even if its counter keeps rising.
        # A different tracked issue is a genuinely new restart candidate.
        "restart_issue_key": (
            health.restart_issue_key
            if reason == "same issue repeated after two interventions"
            else None
        ),
    }
    return compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _runtime_action_signature(action: compat.TriggeringAction) -> str:
    payload = action.model_dump(mode="json")
    return compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _unique_runtime_trigger_actions(
    actions: compat.Any,
) -> list[compat.TriggeringAction]:
    selected: list[compat.TriggeringAction] = []
    seen: set[str] = set()
    for action in actions:
        if action is None:
            continue
        signature = compat._runtime_action_signature(action)
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(action)
    return selected


def _suspicious_changed_file_signature(
    workspace_root: compat.Path,
    changed_files: list[compat.ChangedFile],
    *,
    cache: dict[str, tuple[tuple[compat.Any, ...], str]] | None = None,
) -> str | None:
    suspicious = sorted(
        (changed for changed in changed_files if compat._is_suspicious_changed_path(changed.path)),
        key=lambda item: item.path,
    )
    if not suspicious:
        return None
    cache = cache if cache is not None else {}
    payload = [
        {
            "path": changed.path,
            "status": changed.status,
            "additions": changed.additions,
            "deletions": changed.deletions,
            "content": compat._workspace_path_fingerprint(workspace_root, changed.path, cache=cache),
        }
        for changed in suspicious
    ]
    return compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _workspace_path_fingerprint(
    workspace_root: compat.Path,
    relative_path: str,
    *,
    cache: dict[str, tuple[tuple[compat.Any, ...], str]],
) -> str:
    raw_path = compat.Path(relative_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return "invalid-path"
    path = workspace_root / raw_path
    try:
        lexical_stat = path.lstat()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unavailable:{type(exc).__name__}"
    if compat.stat.S_ISLNK(lexical_stat.st_mode):
        try:
            target = compat.os.readlink(path)
        except OSError as exc:
            target = f"unreadable:{type(exc).__name__}"
        cache.pop(relative_path, None)
        return "symlink:" + compat.hashlib.sha256(target.encode("utf-8", errors="replace")).hexdigest()
    try:
        resolved = path.resolve()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unresolved:{type(exc).__name__}"
    if not compat.ensure_relative_to(resolved, workspace_root):
        cache.pop(relative_path, None)
        return "outside-workspace"
    try:
        file_stat = resolved.stat()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unavailable:{type(exc).__name__}"
    stat_key = (
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_ino,
    )
    if not compat.stat.S_ISREG(file_stat.st_mode):
        fingerprint = compat.hashlib.sha256(repr(stat_key).encode("ascii")).hexdigest()
    else:
        try:
            digest = compat._hash_file(resolved)
            fingerprint = f"regular:{compat.stat.S_IMODE(file_stat.st_mode):o}:{digest}"
        except OSError as exc:
            fingerprint = (
                f"unreadable:{compat.stat.S_IMODE(file_stat.st_mode):o}:{type(exc).__name__}"
            )
    cache[relative_path] = (stat_key, fingerprint)
    return fingerprint


def _hash_file(path: compat.Path) -> str:
    descriptor = compat._open_regular_file_no_follow(path)
    digest = compat.hashlib.sha256()
    try:
        while chunk := compat.os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        compat.os.close(descriptor)
    return digest.hexdigest()


def _workspace_state_id(project_root: compat.Path) -> str:
    root = project_root.resolve()
    digest = compat.hashlib.sha256()
    skip_dirs = {
        ".git",
        ".supervisor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
    }
    for current, dirs, files in compat.os.walk(root, followlinks=False):
        rel_dir = compat.Path(current).relative_to(root)
        traversable_dirs: list[str] = []
        for name in sorted(dirs):
            if name in skip_dirs:
                continue
            path = compat.Path(current) / name
            try:
                metadata = path.lstat()
            except OSError:
                compat._update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
                continue
            if compat.is_link_or_reparse(path, stat_result=metadata):
                compat._update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
            elif compat.stat.S_ISDIR(metadata.st_mode):
                traversable_dirs.append(name)
            else:
                compat._update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
        dirs[:] = traversable_dirs
        for name in sorted(files):
            path = compat.Path(current) / name
            rel = (rel_dir / name).as_posix()
            compat._update_workspace_entry_digest(digest, path, rel)
    return digest.hexdigest()


def _update_workspace_entry_digest(digest: compat.Any, path: compat.Path, relative_path: str) -> None:
    encoded_path = relative_path.encode("utf-8", errors="surrogateescape")
    digest.update(encoded_path)
    digest.update(b"\0")
    try:
        metadata = path.lstat()
        mode = metadata.st_mode
        if compat.is_link_or_reparse(path, stat_result=metadata):
            digest.update(b"symlink\0")
            try:
                target = compat.os.readlink(path)
            except OSError:
                target = "<opaque-reparse-point>"
            digest.update(target.encode("utf-8", errors="surrogateescape"))
        elif compat.stat.S_ISREG(mode):
            flags = compat.os.O_RDONLY | getattr(compat.os, "O_NONBLOCK", 0) | getattr(compat.os, "O_NOFOLLOW", 0)
            descriptor = compat.os.open(path, flags)
            try:
                opened_mode = compat.os.fstat(descriptor).st_mode
                if not compat.stat.S_ISREG(opened_mode):
                    digest.update(f"special:{compat.stat.S_IFMT(opened_mode):o}".encode("ascii"))
                else:
                    digest.update(b"file\0")
                    while chunk := compat.os.read(descriptor, 1024 * 1024):
                        digest.update(chunk)
            finally:
                compat.os.close(descriptor)
        else:
            digest.update(f"special:{compat.stat.S_IFMT(mode):o}".encode("ascii"))
    except OSError:
        digest.update(b"unreadable\0")
    digest.update(b"\0")
