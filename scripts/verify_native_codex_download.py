#!/usr/bin/env python3
"""Cold HTTPS install and unchanged-cache smoke for the published Windows pin."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from supervisor.runtime import native_codex_install as installer
from supervisor.runtime.codex_distiller import validate_native_selection


def snapshot(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in sorted(directory.rglob("*")) if path.is_file()}


def verify(runtime_root: Path) -> dict:
    bundle = installer.BUNDLES.get(("Windows", "x86_64"))
    if bundle is None:
        return {"status": "skipped_no_windows_pin", "passed": None, "published_pin": False}
    if os.name != "nt":
        raise RuntimeError("Published Windows helper smoke requires Windows")
    try:
        runtime_root.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("HTTPS smoke requires a fresh runtime root")

    # Use no user auth/config or native overrides; retain only OS/tool paths.
    clean = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
        "LOCALAPPDATA", "USERNAME"}}
    home = runtime_root / "empty-home"
    clean.update({"HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home),
                  "APPDATA": str(home / "appdata"), "BELLO_RUNTIME_DIR": str(runtime_root / "cache")})
    os.environ.clear()
    os.environ.update(clean)
    installer._private_directory(home, parents=True)

    # This is the production downloader, including HTTPS and both pinned hashes.
    command, manifest = installer.ensure_native_selection()
    if manifest is None or installer._sha256(manifest) != bundle.manifest_sha256:
        raise ValueError("Installed manifest differs from the published pin")
    before = snapshot(manifest.parent)
    capability = asyncio.run(validate_native_selection(command, manifest))
    if installer.ensure_native_selection() != (command, manifest):
        raise ValueError("Repeated installation selected a different cached helper")
    repeated = asyncio.run(validate_native_selection(command, manifest))
    if snapshot(manifest.parent) != before or repeated != capability:
        raise ValueError("Published helper cache changed during reuse")
    return {"status": "passed", "passed": True, "published_pin": True, "cold_cache": True,
            "cache_unchanged": True, "offline_feature_validation": True,
            "url": bundle.url, "archive_sha256": bundle.archive_sha256,
            "manifest_sha256": bundle.manifest_sha256, "binary_sha256": capability["binary_sha256"],
            "cached_files": before, "paid_model_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.absolute()
    result = verify(args.runtime_root.absolute())
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
