#!/usr/bin/env python3
"""Cold production HTTPS install and credential-free native async proof.

Run on each supported platform after publishing its pinned archive. No local
archive substitution, build, login, API key, or paid model request is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from supervisor.runtime import native_codex_install as installer
from supervisor.runtime.codex_distiller import validate_native_selection
from scripts.verify_native_codex_download import snapshot
from scripts.verify_native_codex_async import verify as verify_async

CASES = frozenset({
    "direct_off", "direct_on", "code_off", "code_on", "steer", "interrupt", "child_wait",
    "async_and_selection_direct", "async_and_selection_code",
    "direct_on_repeat_2", "steer_repeat_2", "direct_on_repeat_3", "steer_repeat_3",
})


def validate_proof(proof: dict) -> None:
    rows = proof.get("results", [])
    if (proof.get("schema") != "bello.native-async-smoke.v1"
            or proof.get("passed") is not True or proof.get("paid_model_calls") != 0
            or proof.get("on_off_native_instructions_identical") is not True
            or len(rows) != len(CASES) or {row.get("case") for row in rows} != CASES
            or any(row.get("passed") is not True
                   or row.get("external_proxy_requests_forwarded") != 0 for row in rows)):
        raise ValueError("Published async bundle did not pass every credential-free proof")


async def verify(runtime_root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    result = {"schema": "bello.published-native-async.v1", "passed": False,
              "paid_model_calls": 0, "phase": "preconditions"}
    previous_environment = dict(os.environ)
    try:
        key = installer._platform_key()
        bundle = installer.ASYNC_BUNDLES.get(key)
        if bundle is None:
            raise ValueError("No published async bundle is pinned for this platform")
        if os.path.lexists(runtime_root):
            raise ValueError("Cold install requires a fresh runtime directory")
        allowed = {"PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT",
                   "WINDIR", "PATHEXT", "COMSPEC", "USERNAME"}
        clean = {k: v for k, v in previous_environment.items() if k.upper() in allowed}
        home = runtime_root / "empty-home"
        clean.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home),
                     APPDATA=str(home / "config"), LOCALAPPDATA=str(home / "data"),
                     XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
                     XDG_CACHE_HOME=str(home / "cache"), BELLO_RUNTIME_DIR=str(runtime_root / "cache"),
                     GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        os.environ.clear()
        os.environ.update(clean)
        installer._private_directory(home, parents=True)
        result["phase"] = "https_cold_install"
        command, manifest = installer.ensure_native_async()
        if manifest is None or installer._sha256(manifest) != bundle.manifest_sha256:
            raise ValueError("Downloaded manifest does not match the published pin")
        before = snapshot(manifest.parent)
        capability = await validate_native_selection(command, manifest)
        result.update(platform=list(key), url=bundle.url, archive_sha256=bundle.archive_sha256,
                      manifest_sha256=bundle.manifest_sha256, binary_sha256=capability["binary_sha256"])
        result["phase"] = "native_async_proofs"
        proof = await verify_async(Path(command[0]), output / "async", concurrency_repeats=3)
        validate_proof(proof)
        result["phase"] = "cache_reuse"
        if installer.ensure_native_async() != (command, manifest) or snapshot(manifest.parent) != before:
            raise ValueError("Installed cache changed during execution or reuse")
        result.update(passed=True, phase="complete", cold_cache=True, cache_unchanged=True,
                      cases_passed=len(CASES), proof_sha256=installer._sha256(output / "async/report.json"))
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        os.environ.clear()
        os.environ.update(previous_environment)
    (output / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args.runtime_root.absolute(), args.output_dir.absolute()))
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result["passed"] else 1)
