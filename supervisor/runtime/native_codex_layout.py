"""Check helpers only when a host manifest declares Bello's packaged layout.

Bare host executables and capability-only custom manifests retain their existing
contract. This is a read-only completeness check, not an installer or a claim
that an arbitrary host manifest is a trusted release.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
from typing import Sequence

from supervisor.filesystem_safety import is_link_or_reparse


_MAX_MANIFEST_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def validate_manifest_bundle(executable: Path, manifest_path: Path, manifest: dict) -> None:
    """Verify declared launcher/helpers without imposing layout on custom hosts."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not ({"bin/codex", "bin/codex.exe"} & files.keys()):
        return
    if any(not isinstance(name, str) or "\\" in name or
           PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or
           str(PurePosixPath(name)) != name for name in files):
        raise RuntimeError("Native Codex package manifest has an unsafe member path")
    windows = "bin/codex.exe" in files
    if windows and "bin/codex" in files:
        raise RuntimeError("Native Codex package manifest declares ambiguous launchers")
    launcher = "bin/codex.exe" if windows else "bin/codex"
    required = [launcher, "bin/codex-code-mode-host" + (".exe" if windows else "")]
    if windows:
        required += ["bin/codex-command-runner.exe", "bin/codex-windows-sandbox-setup.exe"]
    elif platform.system() == "Linux":
        required += ["bin/codex-resources/bwrap"]
    # Resolve the host-owned manifest once, not arbitrary paths from its map.
    root = manifest_path.resolve(strict=True).parent
    if executable.resolve(strict=True) != root / launcher:
        raise RuntimeError("Native Codex package manifest does not describe the selected executable layout")
    if manifest.get("binary_sha256") != files[launcher]:
        raise RuntimeError("Native Codex package manifest launcher checksum is inconsistent")
    for relative in required:
        expected = files.get(relative)
        if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            raise RuntimeError(f"Native Codex package manifest is incomplete: {relative}")
        path = root / relative
        try:
            # Never accept a linked helper or a linked nested resource directory.
            members = (parent for parent in (path, *path.parents) if root in parent.parents)
            if any(is_link_or_reparse(parent) for parent in members):
                raise ValueError("linked package member")
            if not path.is_file() or (not windows and not os.access(path, os.X_OK)):
                raise ValueError("missing or non-executable package member")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise ValueError("package member checksum mismatch")
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Native Codex packaged helper is missing or invalid: {relative}. "
                "Restore the complete matching native bundle; copying only codex is insufficient."
            ) from exc


async def validate_native_package(command: Sequence[str], manifest_path: Path | None) -> None:
    """Reject incomplete explicit bundles before app-server/auth/model startup."""
    if manifest_path is None:
        return

    def check() -> None:
        try:
            with manifest_path.open("rb") as stream:
                payload = stream.read(_MAX_MANIFEST_BYTES + 1)
            if len(payload) > _MAX_MANIFEST_BYTES:
                raise ValueError("oversized manifest")
            manifest = json.loads(payload)
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
        except (OSError, ValueError, UnicodeError):
            # Selection-off hosts historically ignore an unusable capability
            # file. Without a readable files map no packaged layout is proven;
            # selection-on retains its existing strict manifest validation.
            return
        files = manifest.get("files")
        if not isinstance(files, dict) or not ({"bin/codex", "bin/codex.exe"} & files.keys()):
            return
        executable = shutil.which(command[0]) if command and isinstance(command[0], str) else None
        if executable is None:
            raise RuntimeError("Native Codex packaged executable was not found")
        validate_manifest_bundle(Path(executable), manifest_path, manifest)

    await asyncio.to_thread(check)
