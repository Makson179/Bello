"""Map Bello's assigned filesystem scope to native Codex permission profiles.

This uses Codex's sandbox, not a second tool gate. ``:minimal`` also exposes
native platform/runtime dependencies, so this is not an exact copy of Pi's
filesystem allowlist. Windows root-read requires explicit host consent and
keeps known private controller/auth directories denied. No shared writable
temporary directory is added.

Apply the returned fields on thread/start and thread/resume, merging ``config``
with the other native settings. Keep the host-owned selector in that config:
native retained-session rebuilding drops the top-level request override.
A profile replaces the legacy ``sandbox`` field.
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


def validate_windows_native_scope(params: Mapping[str, Any]) -> None:
    """Fail before dispatch for Bello scopes rejected by the Windows backend.

    Mirrors native 0.155.1's effective cwd-root read prerequisite, not a search
    for the literal ``:root`` key. This is only for Bello's generated grants:
    user ``config.permissions`` is replaced and runtime paths never grant a
    filesystem root. The backend separately validates its exact private denies.
    It is not an arbitrary native permission-profile or Windows ACL evaluator.
    Call only for Windows; root-read must be an explicit host-owned opt-in.
    """
    # Reuse the mapper's validation and exact host-path normalization. Omitted
    # scratch/runtime grants cannot cover cwd's root: both are bounded below it.
    opt_in = params.get("windowsNativeRootRead", False)
    if type(opt_in) is not bool:
        raise AppServerError("windowsNativeRootRead must be a boolean")
    permissions = native_permission_params(params, windows_root_read=opt_in)
    if "permissions" not in permissions:  # Explicit danger-full-access.
        return
    cwd = Path(_absolute_path(params.get("cwd"), "cwd"))
    root = Path(cwd.anchor)
    if opt_in or any(Path(path) == root for path in permissions["runtimeWorkspaceRoots"]):
        return
    raise AppServerError(
        "unsupported_permission_profile: native Codex on Windows cannot enforce "
        "this scoped-read profile; its elevated sandbox requires effective read "
        "access to the working directory's filesystem root (native 0.155.1). "
        "Bello did not broaden permissions or dispatch this request. "
        "Use a supported execution host, or explicitly choose a broader read "
        "scope; restricted Windows support remains unavailable."
    )


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
    windows_root_read: bool = False, private_read_roots: tuple[Path, ...] = (),
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
    if type(windows_root_read) is not bool:
        raise AppServerError("windows root-read consent must be a boolean")
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
    if windows_root_read:
        filesystem[str(Path(cwd).anchor)] = "read"
    if mode == "workspace-write":
        filesystem[cwd] = "write"
        # Keep the native workspace-write protection of repository controls.
        for name in _READONLY_METADATA:
            filesystem[str(Path(cwd) / name)] = "read"
        if temp_dir is not None:
            filesystem[_absolute_path(temp_dir, "temp_dir")] = "write"
    for path in runtime_read_paths:
        filesystem[_absolute_path(path, "runtime_read_paths")] = "read"
    if windows_root_read:
        for path in private_read_roots:
            private = Path(_absolute_path(path, "private_read_roots"))
            if private == Path(private.anchor) or Path(cwd).is_relative_to(private):
                raise AppServerError("native Windows private read denial overlaps the workspace or filesystem root")
            if temp_dir is not None and Path(temp_dir).is_relative_to(private):
                raise AppServerError("native Windows scratch overlaps private controller state")
            filesystem[str(private)] = "deny"

    return {
        "permissions": PROFILE_ID,
        "runtimeWorkspaceRoots": readable_roots,
        "config": {"default_permissions": PROFILE_ID, "permissions": {PROFILE_ID: {
            "filesystem": filesystem,
            "network": {"enabled": network},
        }}},
    }
