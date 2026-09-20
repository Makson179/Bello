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
import re
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


def history_output_texts(payload: dict, tool_name: str = "other") -> list[str]:
    """Pinned native wire output is text or input_text content, never {body: ...}."""
    value = payload.get("output")
    texts = ([value] if isinstance(value, str) else
             [item["text"] for item in value if isinstance(item, dict)
              and item.get("type") == "input_text" and isinstance(item.get("text"), str)]
             if isinstance(value, list) else [])
    outputs = []
    # Code mode preserves each text(...) emission as its own input_text item.
    # text(result.output) therefore has no JSON/exit metadata around that leaf.
    # Recognize it only after an exact native status header and a correlated
    # exec/wait call. Do not join leaves, normalize newlines, or hash the header.
    correlated_code = ((tool_name == "exec" and payload.get("type") == "custom_tool_call_output")
                       or (tool_name == "wait" and payload.get("type") == "function_call_output"))
    if (correlated_code and isinstance(value, list) and len(value) > 1
            and isinstance(value[0], dict) and value[0].get("type") == "input_text"
            and isinstance(value[0].get("text"), str)
            and re.fullmatch(r"Script (?:completed|failed|terminated|running with cell ID [^\s]+)\n"
                             r"Wall time \d+\.\d seconds\nOutput:\n", value[0]["text"])):
        outputs.extend(item["text"] for item in value[1:] if isinstance(item, dict)
                       and item.get("type") == "input_text" and isinstance(item.get("text"), str))
    for text in texts:
        header, separator, content = text.partition("\nOutput:\n")
        # Unified exec can return selected partial output with a live session,
        # before exit_code exists. This is still a model-facing tool response.
        if separator and re.search(
                r"(?m)^(?:Process exited with code -?\d+|Process running with session ID \d+|Exit code: -?\d+)$",
                header):
            outputs.append(content)
        elif (payload.get("type") == "custom_tool_call_output"
              or (payload.get("type") == "function_call_output" and tool_name == "wait")):
            # Native code-mode exec is custom, but its wait continuation is a
            # function tool with the same structured runtime output. Only allow
            # this form after exact call-ID correlation to the fixed wait tool.
            packet = {**payload, "type": "custom_tool_call_output", "call_id": native.CALL_ID, "output": text}
            for item in native.output_packets({"input": [packet]}, "code"):
                if isinstance(item.get("output"), str):
                    outputs.append(item["output"])
    return list(dict.fromkeys(outputs))


def history_output_shape(payload: dict, tool_name: str = "other") -> dict:
    """Structural diagnostics only: fixed keys/types, never tool text or keys supplied by a model."""
    def kind(value):
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        if value is None:
            return "null"
        return "scalar"
    value = payload.get("output")
    allowed_tools = {"exec_command", "write_stdin", "exec", "wait", "apply_patch", "shell", "shell_command"}
    item_type = payload.get("type")
    shape = {"output_type": kind(value),
             "output_item_type": item_type if item_type in {"function_call_output", "custom_tool_call_output"} else "other",
             "tool_name": tool_name if tool_name in allowed_tools else "other"}
    if isinstance(value, str):
        shape.update(plain_output_sha256=digest(value), lf_newlines=value.count("\n"),
                     crlf_newlines=value.count("\r\n"), lf_output_separator="\nOutput:\n" in value,
                     crlf_output_separator="\r\nOutput:\r\n" in value,
                     completed_header="Process exited with code " in value,
                     running_header="Process running with session ID " in value,
                     canonical_direct_header=bool(re.search(
                         r"(?m)^(?:Process exited with code -?\d+|Process running with session ID \d+|Exit code: -?\d+)$",
                         value)))
        try:
            value = json.loads(value)
        except ValueError:
            value = None
        shape["string_json_type"] = kind(value)
    allowed = {"type", "text", "output", "body", "content", "content_items", "metadata", "success",
               "exit_code", "session_id", "chunk_id", "wall_time_seconds", "encrypted_content"}
    if isinstance(value, dict):
        shape["known_keys"] = {key: kind(value[key]) for key in sorted(allowed & value.keys())}
    elif isinstance(value, list):
        shape["array_types"] = sorted({kind(item) for item in value})
        known_types = {"input_text", "output_text", "text", "input_image", "input_audio", "encrypted_content"}
        shape["known_content_types"] = sorted({item["type"] for item in value if isinstance(item, dict)
            and isinstance(item.get("type"), str) and item["type"] in known_types})
        shape["input_text_shapes"] = [history_output_shape({"type": item_type, "output": item["text"]}, tool_name)
            for item in value if isinstance(item, dict) and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)]
    return shape


def selected_history_evidence(home: Path, thread_id: str, rollout_path: str | None = None) -> tuple[set[str], dict]:
    hashes: set[str] = set()
    counts = {"path_from_server": rollout_path is not None, "files_found": 0, "owned_files": 0,
              "records": 0, "response_items": 0, "tool_outputs": 0, "parsed_outputs": 0,
              "unparsed_tool_outputs": 0, "tool_output_shapes": []}
    if rollout_path is not None:
        candidate = Path(rollout_path).resolve()
        if not candidate.is_relative_to(home.resolve()) or candidate.suffix != ".jsonl":
            raise ValueError("Native rollout path is outside the private smoke home or unsupported")
        paths = [candidate] if candidate.is_file() else []
    else:
        paths = [path for root in (home / "sessions", home / "archived_sessions")
                 for path in root.rglob(f"*{thread_id}.jsonl")]
    for path in dict.fromkeys(path.resolve() for path in paths):
        if not path.is_relative_to(home.resolve()):
            raise ValueError("Native rollout escaped the private smoke home")
        counts["files_found"] += 1
        # Metadata, not the filename, establishes exact ownership. Neither UI
        # aggregatedOutput nor SQLite's presentation projection is evidence.
        with path.open(encoding="utf-8", newline="") as stream:
            records = [json.loads(line) for line in stream if line.strip()]
        if not records or any(not isinstance(record, dict) for record in records):
            raise ValueError("Malformed native rollout")
        metadata = [record.get("payload") for record in records if record.get("type") == "session_meta"]
        if not metadata or any(not isinstance(meta, dict) or meta.get("id") != thread_id for meta in metadata):
            raise ValueError("Native rollout does not belong to the exact smoke thread")
        counts["owned_files"] += 1
        counts["records"] += len(records)
        # Correlate locally, but export neither call IDs nor tool arguments.
        calls = {record["payload"].get("call_id"): record["payload"].get("name")
                 for record in records if record.get("type") == "response_item"
                 and isinstance(record.get("payload"), dict)
                 and record["payload"].get("type") in {"function_call", "custom_tool_call"}
                 and isinstance(record["payload"].get("call_id"), str)}
        for record in records:
            payload = record.get("payload", {})
            if record.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            counts["response_items"] += 1
            if payload.get("type") not in {"function_call_output", "custom_tool_call_output"}:
                continue
            counts["tool_outputs"] += 1
            tool_name = calls.get(payload.get("call_id"), "other")
            outputs = history_output_texts(payload, tool_name)
            counts["parsed_outputs"] += len(outputs)
            counts["unparsed_tool_outputs"] += int(not outputs)
            shape = history_output_shape(payload, tool_name)
            item = next((entry for entry in counts["tool_output_shapes"] if entry["shape"] == shape), None)
            if item is None:
                counts["tool_output_shapes"].append({"shape": shape, "parsed": bool(outputs), "count": 1})
            else:
                item["count"] += 1
            hashes.update(digest(text) for text in outputs)
    return hashes, counts


def selected_history_hashes(home: Path, thread_id: str) -> set[str]:
    return selected_history_evidence(home, thread_id)[0]


def live_checks_pass(result: dict) -> bool:
    reduced = result["reduced_logs"]
    return bool(result["task_passed"] and result["successful_native_check"] and reduced > 0
                and result["exact_selected_native_history_matches"] == reduced
                and result["bridge_outcomes"]["changed"] >= reduced
                and result["worker_failures"] == 0)


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
        rollout_path = reply["thread"].get("path")
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
        # In the pinned paginated implementation includeTurns persists the
        # loaded thread before returning. Read its authoritative path instead
        # of assuming filenames are permanently identical to thread IDs.
        result["phase"] = "history_persist"
        history_reply = await session.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        history_thread = history_reply.get("thread", {})
        if history_thread.get("id") != thread_id:
            raise ValueError("Native history reply belongs to a different thread")
        rollout_path = history_thread.get("path") or rollout_path
        result["history_read_completed"] = True
        result["history_rollout_path_available"] = isinstance(rollout_path, str)
        # Stop and flush before reading. Raw RPC and histories remain private
        # and are deleted with the auth home; export only counts and hashes.
        await session.close(private_logs)
        session = None
        result["phase"] = "history_check"
        hashes, counts = selected_history_evidence(home, thread_id, rollout_path)
        result["native_history_counts"] = counts
        measurements = selector.measurements
        reduced = [m for m in measurements if m["worker_response_ok"] and m["returned_worker_output"] and m["strictly_reduced"]]
        matched = [m for m in reduced if m["selected_sha256"] in hashes]
        result.update(measurements=measurements, distiller_invocations=len(measurements),
                      reduced_logs=len(reduced), exact_selected_native_history_matches=len(matched),
                      bridge_outcomes=dict(bridge.metrics), worker_failures=len(selector.worker_failure_types))
        result["passed"] = live_checks_pass(result)
        result["phase"] = "complete"
    except Exception as exc:
        # RPC/auth errors can contain sensitive provider details. Export type only.
        result["error_type"] = type(exc).__name__
    finally:
        if session is not None:
            await session.close(private_logs)
        await bridge.close()
        await selector.close()
        # Keep these safe counters even if history collection itself failed.
        result["worker_failures"] = len(selector.worker_failure_types)
        result["worker_error_types"] = sorted(set(selector.worker_failure_types))
        result["passed"] = result["passed"] and result["worker_failures"] == 0
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
    checkpoints = {"windows": os.name == "nt", "secret_present": bool(payload),
                   "auth_home_fresh": not os.path.lexists(home)}
    result = {"passed": False, "paid_turns_started": 0, "auth_removed": False,
              "phase": "preconditions", "checkpoints": checkpoints}
    owned_home = False
    auth = None
    try:
        if not all(checkpoints.values()):
            raise ValueError("Live smoke requires Windows, a secret and a fresh auth home")
        result["phase"] = "parse_auth"
        auth = json.loads(payload)
        payload = ""
        if not isinstance(auth, dict) or not auth:
            raise ValueError("Invalid auth payload")
        checkpoints["auth_parsed"] = True
        result["phase"] = "prepare_output"
        output.mkdir(parents=True, exist_ok=False)
        result["phase"] = "isolate_environment"
        clean = {key: value for key, value in os.environ.items() if key.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "USERNAME"}}
        clean.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home), APPDATA=str(home / "appdata"),
                     BELLO_RUNTIME_DIR=str(runtime / "native"), HF_HOME=str(runtime / "huggingface"),
                     HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
        os.environ.clear()
        os.environ.update(clean)
        checkpoints["environment_isolated"] = True
        # LOCALAPPDATA is the same validated private anchor as the offline
        # smoke. RUNNER_TEMP can grant other accounts directory replacement.
        result["phase"] = "prepare_private_runtime"
        _private_directory(runtime, parents=True)
        checkpoints["runtime_private"] = True
        result["phase"] = "prepare_private_auth_home"
        _private_directory(home)
        owned_home = True
        checkpoints["auth_home_private"] = True
        result["phase"] = "write_auth"
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
        checkpoints["auth_written"] = True
        auth.clear()
        result["phase"] = "prepare_workspace"
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\nweb_search = "disabled"\n', encoding="utf-8")
        _private_directory(home / "tmp")
        _private_directory(home / "private-logs")
        work = runtime / "live-workspace"
        _private_directory(work)
        (work / "solution.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (work / "check.py").write_text(CHECKER, encoding="utf-8")
        checkpoints["workspace_ready"] = True
        result["phase"] = "download_native_helper"
        command, manifest = ensure_native_selection()
        result["phase"] = "validate_native_helper"
        asyncio.run(validate_native_selection(command, manifest))
        checkpoints["native_ready"] = True
        result["phase"] = "download_model"
        bundle = ensure_default_bundle()
        checkpoints["model_ready"] = True
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        result["phase"] = "live_turn"
        result.update(asyncio.run(live_turn(Path(command[0]), bundle, home, work, home / "private-logs")))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        payload = ""
        if isinstance(auth, dict):
            auth.clear()
        if owned_home:
            try:
                remove_path_tree(home)
            except Exception as exc:
                result["cleanup_error_type"] = type(exc).__name__
        result["auth_removed"] = not os.path.lexists(home)
        result["passed"] = result["passed"] and result["auth_removed"]
        if output.is_dir():
            (output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
