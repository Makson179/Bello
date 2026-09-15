"""Map Bello's assigned filesystem scope to native Codex permission profiles.

This uses Codex's sandbox, not a second tool gate. ``:minimal`` also exposes
native platform/runtime dependencies, so this is not an exact copy of Pi's
filesystem allowlist. No user home or shared temporary directory is added.

Apply the returned fields on thread/start and thread/resume, merging ``config``
with the other native settings. A profile replaces the legacy ``sandbox`` field.
Subsequent turns must inherit it: a legacy ``sandboxPolicy`` would replace it,
while reselecting ``permissions`` would reload global rather than thread config.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

from supervisor.appserver import AppServerError


PROFILE_ID = "bello-native"
_READONLY_METADATA = (".git", ".agents", ".codex")


def _absolute_path(value: Any, field: str) -> str:
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise AppServerError(f"native Codex {field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise AppServerError(f"native Codex {field} must be an absolute path")
    # Paths belong to the native execution host. Do not resolve them against
    # unrelated files on a caller's filesystem or follow symlinks here.
    return os.path.normpath(str(path))


def native_permission_params(
    params: Mapping[str, Any], *, temp_dir: Path | None = None,
    runtime_read_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Return native thread fields for the existing, host-assigned scope.

    ``temp_dir`` must be the backend-owned per-run scratch directory, separate
    from its Codex home/auth directory. The backend creates it and sets its
    child's TMPDIR/TMP/TEMP to the same path. This helper does no filesystem IO.
    Read-only threads do not acquire writable scratch space. Explicit
    danger-full-access keeps native legacy behavior without a custom profile.
    ``runtime_read_paths`` are exact executables and narrow toolchain roots
    resolved by the host, never tool/model arguments. Native ``:minimal`` does
    not cover every installed interpreter, SDK or launcher symlink target.
    """
    mode = params.get("sandbox", "workspace-write")
    if mode == "danger-full-access":
        return {"sandbox": mode}
    if not isinstance(mode, str) or mode not in {"read-only", "workspace-write"}:
        raise AppServerError("unsupported native Codex filesystem scope")

    cwd = _absolute_path(params.get("cwd"), "cwd")
    roots = params.get("runtimeWorkspaceRoots")
    if roots is None:
        roots = []
    if not isinstance(roots, (list, tuple)):
        raise AppServerError("native Codex runtimeWorkspaceRoots must be a path list")
    readable_roots = list(dict.fromkeys([
        cwd, *(_absolute_path(root, "runtimeWorkspaceRoots") for root in roots),
    ]))
    network = params.get("networkAccess", False)
    if not isinstance(network, bool):
        raise AppServerError("native Codex networkAccess must be a boolean")

    filesystem = {":minimal": "read", ":workspace_roots": "read"}
    if mode == "workspace-write":
        filesystem[cwd] = "write"
        # Keep the native workspace-write protection of repository controls.
        for name in _READONLY_METADATA:
            filesystem[str(Path(cwd) / name)] = "read"
        if temp_dir is not None:
            filesystem[_absolute_path(temp_dir, "temp_dir")] = "write"
    for path in runtime_read_paths:
        filesystem[_absolute_path(path, "runtime_read_paths")] = "read"

    return {
        "permissions": PROFILE_ID,
        "runtimeWorkspaceRoots": readable_roots,
        "config": {"permissions": {PROFILE_ID: {
            "filesystem": filesystem,
            "network": {"enabled": network},
        }}},
    }
