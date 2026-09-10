"""Install the pinned worker dependencies separately from the user's projects."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from packaging.version import InvalidVersion, Version

from supervisor.executables import require_trusted_executable
from supervisor.state import FileLock


MIN_NODE_VERSION = Version("22.19.0")
PINNED_PI_VERSION = "0.85.1"


def _verify_installed_version(directory: Path) -> None:
    manifest = directory / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("Pi installation is incomplete. Run `bello runtime install`.") from exc
    if version != PINNED_PI_VERSION:
        raise RuntimeError("Pi installation version does not match Bello's pinned runtime")


def source_worker_directory() -> Path:
    return Path(__file__).resolve().parent.parent / "pi_worker"


def node_executable() -> str:
    candidate = os.environ.get("BELLO_NODE", "node")
    executable = require_trusted_executable(candidate, cwd=Path.cwd())
    probe = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, check=True)
    try:
        version = Version(probe.stdout.strip().removeprefix("v"))
    except InvalidVersion as exc:
        raise RuntimeError("could not identify the Node.js version") from exc
    if version < MIN_NODE_VERSION:
        raise RuntimeError(f"Pi requires Node.js >= {MIN_NODE_VERSION}; found {version}. Set BELLO_NODE to the supported executable.")
    return executable


def worker_directory() -> Path:
    source = source_worker_directory()
    if (source / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json").is_file():
        return source
    fingerprint = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file() and "node_modules" not in path.parts and path.suffix in {".mjs", ".json"}:
            fingerprint.update(str(path.relative_to(source)).encode())
            fingerprint.update(path.read_bytes())
    base = Path(os.environ.get("BELLO_RUNTIME_DIR", str(Path.home() / ".bello" / "runtime")))
    return base / "pi" / fingerprint.hexdigest()[:20]


def worker_command() -> list[str]:
    node = node_executable()
    directory = worker_directory()
    entry = directory / "worker.mjs"
    if not entry.is_file() or not (directory / "node_modules").is_dir():
        raise RuntimeError("Pi dependencies are not installed. Run `bello runtime install`.")
    _verify_installed_version(directory)
    return [node, str(entry)]


def _installation_inputs() -> tuple[str, Path, Path]:
    node = node_executable()
    source = source_worker_directory()
    if not (source / "package-lock.json").is_file():
        raise RuntimeError("the pinned Pi dependency lockfile is missing from this Bello installation")
    destination = worker_directory()
    if destination.is_symlink():
        raise RuntimeError("Pi runtime directory must not be a symbolic link")
    return node, source, destination


def _installation_lock(destination: Path) -> FileLock:
    # Keep the lock outside node_modules and the fingerprint directory: npm ci
    # replaces dependencies, and an incomplete installation must be retryable.
    path = destination.parent / f".{destination.name}.install.lock"
    if path.is_symlink():
        raise RuntimeError("Pi runtime installation lock must not be a symbolic link")
    return FileLock(path)


def _install_worker(node: str, source: Path, destination: Path) -> Path:
    if destination.is_symlink():
        raise RuntimeError("Pi runtime directory must not be a symbolic link")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination != source:
        for path in source.rglob("*"):
            if "node_modules" in path.parts or "test" in path.parts or "tests" in path.parts:
                continue
            if path.is_file():
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    npm = require_trusted_executable("npm", cwd=destination)
    env = dict(os.environ)
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=destination, env=env, check=True)
    _verify_installed_version(destination)
    return destination


def _worker_ready(node: str, destination: Path) -> bool:
    entry = destination / "worker.mjs"
    if destination.is_symlink():
        raise RuntimeError("Pi runtime directory must not be a symbolic link")
    if not entry.is_file() or not (destination / "node_modules").is_dir():
        return False
    try:
        _verify_installed_version(destination)
    except RuntimeError:
        return False
    # Starting with closed input only imports the worker and exits. Without an
    # initialize message it neither reads provider credentials nor creates a
    # model runtime. This catches missing transitive dependencies as well as an
    # incomplete source copy, which a package.json version alone cannot detect.
    try:
        result = subprocess.run(
            [node, str(entry)],
            cwd=destination,
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A slow host does not prove corruption; do not reinstall dependencies
        # underneath an otherwise usable (possibly active) runtime on timeout.
        raise RuntimeError("Pi runtime readiness check timed out; its files were left unchanged") from exc
    return result.returncode == 0


def ensure_worker() -> Path:
    """Prepare this release's pinned worker, reusing a usable installation.

    This is the update path. Unlike an explicit runtime reinstall, it must not
    run npm ci in a working runtime that an active Bello run may still use.
    Previous release fingerprints are never removed or changed.
    """
    node, source, destination = _installation_inputs()
    with _installation_lock(destination):
        if _worker_ready(node, destination):
            return destination
        _install_worker(node, source, destination)
        if not _worker_ready(node, destination):
            raise RuntimeError("Pi runtime is not ready after installation. Run `bello runtime install` to retry.")
        return destination


def install_worker() -> Path:
    node, source, destination = _installation_inputs()
    with _installation_lock(destination):
        return _install_worker(node, source, destination)
