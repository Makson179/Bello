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


def install_worker() -> Path:
    node = node_executable()
    source = source_worker_directory()
    if not (source / "package-lock.json").is_file():
        raise RuntimeError("the pinned Pi dependency lockfile is missing from this Bello installation")
    destination = worker_directory()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise RuntimeError("Pi runtime directory must not be a symbolic link")
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
