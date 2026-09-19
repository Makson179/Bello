#!/usr/bin/env python3
"""Published Linux helper: real HTTPS cold install, cache reuse and offline proofs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_native_codex_linux as linux
from scripts import verify_native_codex_selection as native
from scripts.verify_native_codex_download import snapshot
from supervisor.runtime import native_codex_install as installer
from supervisor.runtime.codex_distiller import validate_native_selection


async def verify(runtime_root: Path, output: Path) -> dict:
    result = {"schema": "bello.native-linux-published-smoke.v1", "passed": False,
              "published_pin": True, "paid_model_calls": 0, "phase": "preconditions"}
    output.mkdir(parents=True, exist_ok=False)
    try:
        if platform.system() != "Linux" or platform.machine().lower() != "x86_64":
            raise ValueError("Published helper smoke requires Linux x86_64")
        bundle = installer.BUNDLES.get(("Linux", "x86_64"))
        if bundle is None:
            result["published_pin"] = False
            raise ValueError("No approved published Linux helper pin")
        if os.path.lexists(runtime_root):
            raise ValueError("Published helper smoke requires a fresh runtime root")
        home = runtime_root / "empty-home"
        clean = {key: value for key, value in os.environ.items()
                 if key in {"PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL"}}
        clean.update(HOME=str(home), CODEX_HOME=str(home),
                     XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
                     XDG_CACHE_HOME=str(home / "cache"), BELLO_RUNTIME_DIR=str(runtime_root / "cache"),
                     GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        os.environ.clear()
        os.environ.update(clean)
        installer._private_directory(home, parents=True)
        result["phase"] = "https_cold_install"
        # Use the shipped pin and downloader unchanged, never a local archive.
        command, manifest = installer.ensure_native_selection()
        if manifest is None or installer._sha256(manifest) != bundle.manifest_sha256:
            raise ValueError("Installed manifest differs from the published pin")
        before = snapshot(manifest.parent)
        capability = await validate_native_selection(command, manifest)
        if installer.ensure_native_selection() != (command, manifest) or snapshot(manifest.parent) != before:
            raise ValueError("Published helper cache changed during initial reuse")
        result.update(cold_cache=True, cache_unchanged_before_proofs=True,
                      url=bundle.url, archive_sha256=bundle.archive_sha256,
                      manifest_sha256=bundle.manifest_sha256, binary_sha256=capability["binary_sha256"])
        binary = Path(command[0])
        result["phase"] = "nine_provider_cases"
        provider_output = output / "provider"
        code = await native.main_async(argparse.Namespace(codex=binary, output_dir=provider_output, cases=None))
        proof_path = provider_output / "report.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if code != 0:
            raise ValueError("Published helper provider smoke failed")
        linux.validate_proof(proof, binary)
        result["provider_cases_passed"] = len(proof["cases"])
        result["phase"] = "bundled_sandbox"
        sandbox = linux.sandbox_proof(binary, output / "sandbox")
        linux.validate_sandbox_proof(sandbox, binary)
        result["bundled_sandbox_passed"] = True
        result["phase"] = "post_proof_cache_reuse"
        if installer.ensure_native_selection() != (command, manifest):
            raise ValueError("Published helper cache changed after actual execution")
        repeated = await validate_native_selection(command, manifest)
        if snapshot(manifest.parent) != before or repeated != capability:
            raise ValueError("Cached helper bytes or timestamps changed after execution")
        result.update(passed=True, phase="complete", post_proof_cache_unchanged=True,
                      cached_files=before, provider_proof_sha256=linux.sha256(proof_path),
                      sandbox_proof_sha256=linux.sha256(output / "sandbox/report.json"))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    (output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(verify(args.runtime_root.absolute(), args.output_dir.absolute()))
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
