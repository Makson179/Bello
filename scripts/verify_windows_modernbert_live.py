#!/usr/bin/env python3
"""One opt-in subscription Codex turn in a disposable Windows workspace.

Auth is accepted only via the dedicated CI secret, removed from the environment,
stored in an owner-private temporary Codex home, and deleted before returning.
Only a small allowlisted receipt is exported; never auth, raw RPC or environments.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify_native_codex_selection as native
from scripts.verify_windows_modernbert import ObservedDistiller, digest
from supervisor.filesystem_safety import remove_path_tree
from supervisor.runtime.codex_distiller import CodexDistillerBridge, FOCUS_GUIDANCE, validate_native_selection
from supervisor.runtime.codex_permissions import native_permission_params
from supervisor.runtime.distiller_download import ensure_default_bundle, MODEL_REVISION
from supervisor.runtime.native_codex_install import _private_directory, ensure_native_selection

SECRET_ENV = "BELLO_WINDOWS_SMOKE_AUTH_20260919"
MODEL = "gpt-5.6-luna"
CORRECT = "def add(a, b):\n    return a + b\n"
CHECKER = '''from solution import add
import sys
actual = add(1, 2)
for i in range(120):
    print("PASS cached check: 1 2 3 4 5 6 7 8 9")
if actual != 3:
    print("FAILED test_addition")
    print(f"AssertionError: expected 3, received {actual}")
    sys.exit(1)
print("SMOKE_TESTS_PASSED")
'''


def solution_passes(work: Path) -> bool:
    """Verify exact safe syntax without executing model-generated host code."""
    try:
        return (ast.dump(ast.parse((work / "solution.py").read_text(encoding="utf-8"))) == ast.dump(ast.parse(CORRECT))
                and (work / "check.py").read_text(encoding="utf-8") == CHECKER)
    except (OSError, SyntaxError, UnicodeError):
        return False


def selected_history_hashes(home: Path, thread_id: str) -> set[str]:
    hashes = set()
    # The fresh home has only this run; still bind every read to its exact ID.
    for root in (home / "sessions", home / "archived_sessions"):
        for path in root.rglob(f"*{thread_id}.jsonl"):
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    payload = record.get("payload", {})
                    if record.get("type") != "response_item" or not isinstance(payload, dict):
                        continue
                    if payload.get("type") not in {"function_call_output", "custom_tool_call_output"}:
                        continue
                    packet = {**payload, "call_id": native.CALL_ID}
                    mode = "direct" if payload["type"] == "function_call_output" else "code"
                    for item in native.output_packets({"input": [packet]}, mode):
                        hashes.add(digest(item["output"]))
    return hashes


async def live_turn(binary: Path, bundle: Path, home: Path, work: Path, private_logs: Path) -> dict:
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(bundle), local_files_only=True, use_fast=True, trust_remote_code=False)
    selector = ObservedDistiller(bundle, tokenizer)
    bridge = CodexDistillerBridge(selector, private_logs / "bridge", work)
    session = None
    result = {"model": MODEL, "effort": "low", "service_tier": "default", "paid_turns_started": 0, "phase": "bootstrap",
              "passed": False, "model_revision": MODEL_REVISION, "live_coder_task": True}
    started = time.perf_counter()
    try:
        await bridge.start()
        env = native.isolated_environment(home, binary, 1, bridge.environment)
        for key in list(env):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                del env[key]
        env["PATH"] = str(Path(sys.executable).parent) + ";" + env["PATH"]
        tool_tmp = work.parent / "live-tool-tmp"
        _private_directory(tool_tmp)
        env.update({key: str(tool_tmp) for key in ("TMPDIR", "TMP", "TEMP")})
        session = native.NativeSession(binary, home, env)
        await native.provision_windows_sandbox(binary, home, env, private_logs)
        await session.start()
        permissions = native_permission_params({"cwd": str(work), "sandbox": "workspace-write", "networkAccess": False},
            temp_dir=tool_tmp, runtime_read_paths=(binary, Path(sys.executable).parent))
        config = permissions.pop("config")
        config.update({"windows": {"sandbox": "elevated"}, "model_reasoning_effort": "low",
                       "features.bello_native_selection": True, "features.code_mode": False,
                       "features.code_mode_only": False, "features.shell_zsh_fork": False, "web_search": "disabled"})
        result["phase"] = "thread_start"
        reply = await session.request("thread/start", {"model": MODEL, "modelProvider": "openai",
            "serviceTier": "default", "cwd": str(work), "approvalPolicy": "never", "ephemeral": False,
            "developerInstructions": FOCUS_GUIDANCE, "config": config, **permissions})
        if reply.get("model") not in (None, MODEL) or reply.get("reasoningEffort") not in (None, "low"):
            raise ValueError("Native execution profile differs from the requested smoke")
        thread_id = reply["thread"]["id"]
        task = ("This is a tiny isolated coding smoke. First run `python check.py` with exec_command and a short focus "
                "to identify the failing assertion. Then fix only solution.py so add(a, b) returns a + b. "
                "Run `python check.py` again and finish with a brief result. Do not edit check.py, read it directly, "
                "use the internet, inspect credentials, or touch files outside this workspace.")
        result["phase"] = "turn_start"
        await session.request("turn/start", {"threadId": thread_id, "effort": "low", "serviceTier": "default",
            "input": [{"type": "text", "text": task, "text_elements": []}]})
        result["paid_turns_started"] = 1
        result["phase"] = "turn_running"
        await session.complete(thread_id, timeout=600)
        result["turn_completed"] = True
        result["task_passed"] = solution_passes(work)
        completed = [event.get("params", {}).get("item", {}) for event in session.transcript
                     if event.get("method") == "item/completed"]
        result["successful_native_check"] = any(item.get("type") == "commandExecution"
            and item.get("exitCode") == 0 and "SMOKE_TESTS_PASSED" in (item.get("aggregatedOutput") or "")
            for item in completed)
        # Stop and flush the exact owned native history before inspecting it.
        await session.close(private_logs)
        session = None
        result["phase"] = "history_check"
        hashes = selected_history_hashes(home, thread_id)
        measurements = selector.measurements
        reduced = [m for m in measurements if m["worker_response_ok"] and m["returned_worker_output"] and m["strictly_reduced"]]
        matched = [m for m in reduced if m["selected_sha256"] in hashes]
        result.update(measurements=measurements, distiller_invocations=len(measurements),
                      reduced_logs=len(reduced), exact_selected_native_history_matches=len(matched),
                      bridge_outcomes=dict(bridge.metrics))
        result["passed"] = bool(result["task_passed"] and result["successful_native_check"]
                                and reduced and len(matched) == len(reduced)
                                and bridge.metrics["changed"] >= len(reduced))
        result["phase"] = "complete"
    except Exception as exc:
        # RPC/auth errors can contain sensitive provider details. Export type only.
        result["error_type"] = type(exc).__name__
    finally:
        if session is not None:
            await session.close(private_logs)
        await bridge.close()
        await selector.close()
        result["elapsed_seconds"] = time.perf_counter() - started
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--auth-home", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = os.environ.pop(SECRET_ENV, "")
    runtime, home, output = args.runtime_root.absolute(), args.auth_home.absolute(), args.output_dir.absolute()
    result = {"passed": False, "paid_turns_started": 0, "auth_removed": False}
    owned_home = False
    try:
        if os.name != "nt" or not payload or os.path.lexists(home):
            raise ValueError("Live smoke requires Windows, a secret and a fresh auth home")
        auth = json.loads(payload)
        payload = ""
        if not isinstance(auth, dict) or not auth:
            raise ValueError("Invalid auth payload")
        output.mkdir(parents=True, exist_ok=False)
        clean = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "USERNAME"}}
        clean.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home), APPDATA=str(home / "appdata"),
                     BELLO_RUNTIME_DIR=str(runtime / "native"), HF_HOME=str(runtime / "huggingface"),
                     HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
        os.environ.clear()
        os.environ.update(clean)
        _private_directory(home, parents=True)
        owned_home = True
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
        auth.clear()
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\nweb_search = "disabled"\n', encoding="utf-8")
        _private_directory(home / "tmp")
        _private_directory(home / "private-logs")
        work = runtime / "live-workspace"
        _private_directory(work)
        (work / "solution.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (work / "check.py").write_text(CHECKER, encoding="utf-8")
        command, manifest = ensure_native_selection()
        asyncio.run(validate_native_selection(command, manifest))
        bundle = ensure_default_bundle()
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        result = asyncio.run(live_turn(Path(command[0]), bundle, home, work, home / "private-logs"))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        if owned_home:
            remove_path_tree(home)
        result["auth_removed"] = owned_home and not os.path.lexists(home)
        result["passed"] = result["passed"] and result["auth_removed"]
        if output.is_dir():
            (output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
