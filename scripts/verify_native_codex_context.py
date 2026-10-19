#!/usr/bin/env python3
"""Credential-free native context-compaction proof against a loopback provider.

Reports hashes, counts and assertions only. Model prompts and source history
stay in memory or the synthetic native home's private rollout archive.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_native_codex_async import ScriptedProvider, delivered, tool, unique_tool_resolutions
from scripts.verify_native_codex_selection import NativeSession, isolated_environment, provision_windows_sandbox, windows_permission_params, windows_shell

INITIAL = "Run only the synthetic context fixture; preserve this exact initial requirement."
CORRECTION = "Latest user correction: preserve RELEASE_83 and never publish anything."
INSPECT = "Inspect the retained context and stop; do not execute another command."
SUMMARY_MARKER = "CONTEXT_SUMMARY_"
# The helper asks for plain text; the loopback provider recognizes that request.
SUMMARY_PROMPT = "compact plain-text handoff"
SUMMARY_TARGET_CHARS = 16000
SUMMARY_MAX_CHARS = 48000
ROUNDS = 8
THRESHOLD = 12000


def message_text(item):
    return "\n".join(part.get("text", "") for part in (item.get("content") or []) if isinstance(part, dict))


def is_summary(request):
    return not request.get("tools") and any(
        SUMMARY_PROMPT in message_text(item)
        for item in request.get("input", []) if item.get("type") == "message")


def summary_items(request):
    return [item for item in request.get("input", [])
            if item.get("type") == "message" and SUMMARY_MARKER in message_text(item)]


def source_markers(request):
    serialized = json.dumps(request.get("input", []))
    return {number for number in range(1, ROUNDS + 1) if f"ARCHIVED_RAW_ROUND_{number}_" in serialized}


def fixture_text(number):
    return f"ARCHIVED_RAW_ROUND_{number}_\n" + "fixture evidence 0123456789\n" * 800


def reasoning(number):
    return {"type": "reasoning", "id": f"reasoning-round-{number}",
            "summary": [{"type": "summary_text", "text": f"Preserve complete round {number}."}],
            "encrypted_content": f"synthetic-encrypted-round-{number}"}


def pairs_intact(request):
    calls = {item.get("call_id") for item in request.get("input", [])
             if item.get("type") in {"function_call", "custom_tool_call"}}
    late_ids = []
    for item in request.get("input", []):
        text = message_text(item)
        if item.get("type") == "message" and text.startswith("<bello_async_tool_result>\nCompleted call_id: "):
            try:
                late_ids.append(json.loads(text.splitlines()[1].removeprefix("Completed call_id: ")))
            except (ValueError, IndexError):
                return False
    return unique_tool_resolutions(request) and all(call_id in calls for call_id in late_ids) and all(
        item.get("call_id") in calls for item in request.get("input", [])
        if item.get("type") in {"function_call_output", "custom_tool_call_output"})


def sse(number, items, *, incomplete=False, usage=0):
    response_id = f"context-response-{number}"
    events = [{"type": "response.created", "response": {"id": response_id}}]
    events.extend({"type": "response.output_item.done", "item": item} for item in items)
    response = {"id": response_id, "usage": {"input_tokens": usage, "output_tokens": 10, "total_tokens": usage + 10}}
    if incomplete:
        response["incomplete_details"] = {"reason": "interrupted"}
    events.append({"type": "response.incomplete" if incomplete else "response.completed", "response": response})
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


class ReceiptSession(NativeSession):
    """Keep the shared RPC transport but do not export its raw transcript."""
    async def close(self, _output):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        await asyncio.gather(self.reader, return_exceptions=True)
        stderr = await self.stderr
        self.stderr_receipt = {"bytes": len(stderr), "sha256": hashlib.sha256(stderr).hexdigest()}

    async def terminal(self, thread_id, timeout=90):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.notifications.get()
                if event.get("method") == "fixture/error":
                    raise RuntimeError("Unexpected approval or dynamic-tool request")
                params = event.get("params", {})
                if event.get("method") == "turn/completed" and params.get("threadId") == thread_id:
                    return params["turn"].get("status")


class ContextProvider(ScriptedProvider):
    def __init__(self, workspace, *, enabled, failure=None, pending=False, legacy_envelope=False):
        super().__init__(enabled=enabled, code=False, workspace=workspace)
        self.failure, self.pending, self.legacy_envelope = failure, pending, legacy_envelope
        self.regular, self.summaries, self.regular_summary_counts = [], [], []
        self.regular_rounds = []
        self.emitted_calls = {}
        self.second_requested, self.allow_second = threading.Event(), threading.Event()
        self.summary_while_pending = False
        self.pending_completed_during_summary = False
        self.slow_started = False
        self.round = 0

    def command(self, number):
        if os.name == "nt":
            command = (f"[IO.File]::AppendAllText('execution-{number}.txt', 'x'); "
                       f"Get-Content -Raw history-{number}.data")
            extra = {"shell": windows_shell()}
        else:
            command = f"printf x >> execution-{number}.txt; cat history-{number}.data"
            extra = {}
        return tool(f"round-{number}", "exec_command", {
            "cmd": command, "yield_time_ms": 30000, "max_output_tokens": 16000, "login": False, **extra})

    def response(self, request):
        number = len(self.requests)
        incomplete = False
        if is_summary(request):
            self.summaries.append(request)
            if self.pending and self.slow_started and not (self.workspace / "slow.completed").exists():
                self.summary_while_pending = True
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not (self.workspace / "slow.completed").exists():
                    time.sleep(0.02)
                self.pending_completed_during_summary = (self.workspace / "slow.completed").exists()
            # Plain text, as requested. Nothing here is JSON or headed unless a
            # case deliberately replays the legacy envelope a model may still emit.
            text = f"{SUMMARY_MARKER}{len(self.summaries)} Goal retained; earlier fixture evidence checked; preserve RELEASE_83 and never publish anything."
            if self.failure == "empty":
                text = ""
            elif self.failure == "refused":
                text = "I cannot summarize this conversation."
            elif self.failure == "oversized":
                text = "x" * (SUMMARY_MAX_CHARS + 1)  # rejected, never truncated
            elif self.failure == "truncated":
                incomplete = True
            if self.legacy_envelope:
                text = json.dumps({"status": "complete", "summary": text})
            items = [{"type": "message", "id": f"summary-{number}", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]}]
        else:
            self.regular.append(request)
            self.regular_summary_counts.append(len(self.summaries))
            self.regular_rounds.append(self.round)
            if len(self.regular) == 2 and not self.failure:
                self.second_requested.set()
                if not self.allow_second.wait(15):
                    raise RuntimeError("Correction injection did not finish")
            inspect = any(message_text(item) == INSPECT for item in request.get("input", []) if item.get("type") == "message")
            if not inspect and self.round < ROUNDS:
                self.round += 1
                call = self.command(self.round)
                self.emitted_calls[self.round] = call
                items = [reasoning(self.round), call]
                if self.pending and self.round == 3:
                    self.slow_started = True
                    if os.name == "nt":
                        command = "Start-Sleep -Seconds 8; [IO.File]::WriteAllText('slow.completed', 'done'); [Console]::Out.Write('CONTEXT_SLOW_DONE')"
                        extra = {"shell": windows_shell()}
                    else:
                        command = "sleep 8; printf done > slow.completed; printf CONTEXT_SLOW_DONE"
                        extra = {}
                    items.append(tool("pending-slow", "exec_command", {"cmd": command, "yield_time_ms": 1,
                        "max_output_tokens": 200, "login": False, **extra}))
            else:
                items = [{"type": "message", "id": f"final-{number}", "role": "assistant",
                          "content": [{"type": "output_text", "text": "Context fixture inspected."}]}]
        self.tools.extend(item["name"] for item in items if "name" in item)
        usage = (len(json.dumps(request.get("input", []))) + len(request.get("instructions", ""))) // 4
        return sse(number, items, incomplete=incomplete, usage=usage)


def evaluate(provider, *, enabled, failure, status, resumed, archive_present, execution_counts):
    regular = provider.regular
    first = regular[0].get("input", []) if regular else []
    prefix = bool(first) and all(request.get("input", [])[:len(first)] == first for request in regular)
    same_settings = bool(regular) and all(
        summary.get(key) == regular[0].get(key)
        for summary in provider.summaries for key in ("model", "reasoning", "service_tier"))
    no_tools = all(not request.get("tools") for request in provider.summaries)
    # Plain text: the summarizer is never forced into a structured-output schema
    # and is told the 16,000-character target (the 48,000 maximum is the host's).
    plain_text = all("format" not in (request.get("text") or {}) for request in provider.summaries)
    names_target = all(
        f"{SUMMARY_TARGET_CHARS} characters" in " ".join(
            message_text(item) for item in request.get("input", []) if item.get("type") == "message")
        for request in provider.summaries)
    pairs = all(pairs_intact(request) for request in regular)
    correction_seen = False
    correction_preserved = True
    for request in regular:
        present = sum(message_text(item) == CORRECTION for item in request.get("input", []) if item.get("type") == "message")
        if correction_seen and present != 1:
            correction_preserved = False
        correction_seen |= present == 1
    stable_between = all(
        right.get("input", [])[:len(left.get("input", []))] == left.get("input", [])
        for index, (left, right) in enumerate(zip(regular, regular[1:]))
        if provider.regular_summary_counts[index] == provider.regular_summary_counts[index + 1])
    latest_only = all(len(summary_items(request)) <= 1 for request in regular)
    remaining = source_markers(regular[-1]) if regular else set()
    raw_removed = bool(remaining) and len(remaining) < provider.round
    if not provider.pending:
        raw_removed &= 1 not in remaining
    rollback = bool(regular) and not any(summary_items(request) for request in regular) and source_markers(regular[-1]) == set(range(1, provider.round + 1))
    whole_tail = True
    unchanged_blocks = True
    observed = {}
    for index, request in enumerate(regular):
        retained = request.get("input", [])
        for item in retained:
            if item.get("type") in {"function_call", "function_call_output", "reasoning"}:
                key = (item["type"], item.get("call_id", item.get("encrypted_content")))
                if key in observed and item != observed[key]:
                    unchanged_blocks = False
                observed.setdefault(key, item)
        number = provider.regular_rounds[index]
        if number:
            # This round was emitted after the preceding regular request and
            # is the indivisible newest tail, even if its output exceeds 10%.
            expected = provider.emitted_calls[number]
            calls = [item for item in retained if item.get("type") == "function_call"
                     and item.get("call_id") == expected["call_id"]]
            results = [item for item in retained if item.get("type") == "function_call_output"
                       and item.get("call_id") == expected["call_id"]]
            thoughts = [item for item in retained if item.get("type") == "reasoning"
                        and item.get("encrypted_content") == reasoning(number)["encrypted_content"]]
            whole_tail &= (len(calls) == len(results) == len(thoughts) == 1
                           and all(calls[0].get(key) == expected[key] for key in ("name", "arguments", "call_id"))
                           and thoughts[0].get("summary") == reasoning(number)["summary"])
            if not provider.pending:
                whole_tail &= fixture_text(number) in results[0].get("output", "") if results else False
    exact_once = bool(execution_counts) and all(value == 1 for value in execution_counts)
    if failure:
        outcome = status == "failed" and len(provider.summaries) == 2 and rollback and resumed
    elif enabled:
        outcome = status == "completed" and len(provider.summaries) >= 2 and latest_only and raw_removed and resumed
    else:
        outcome = status == "completed" and not provider.summaries and rollback and resumed
    pending_deliveries = [delivered(request, "CONTEXT_SLOW_DONE") for request in regular]
    pending = not provider.pending or (provider.summary_while_pending and provider.pending_completed_during_summary
        and 1 in pending_deliveries and all(count <= 1 for count in pending_deliveries))
    checks = {"outcome": outcome, "prefix_unchanged": prefix, "same_model_settings": same_settings,
        "summary_tools_disabled": no_tools, "summary_request_is_plain_text": plain_text,
        "summary_prompt_names_character_target": names_target, "call_result_pairs_intact": pairs,
        "append_only_between_compactions": stable_between, "one_current_summary": latest_only,
        "latest_correction_preserved": correction_preserved and (correction_seen or bool(failure)),
        "whole_oversized_latest_round_retained": whole_tail,
        "retained_native_blocks_unchanged": unchanged_blocks, "commands_executed_once": exact_once,
        "raw_history_archived_privately": archive_present, "pending_tool_survives_summary": pending}
    return checks


async def verify_case(binary, output, *, enabled=True, failure=None, pending=False, legacy_envelope=False):
    output.mkdir(parents=True, exist_ok=False)
    home, workspace = output / "private-native-home", output / "work"
    home.mkdir(); workspace.mkdir()
    for number in range(1, ROUNDS + 1):
        (workspace / f"history-{number}.data").write_text(fixture_text(number))
    provider = ContextProvider(workspace, enabled=enabled, failure=failure, pending=pending,
                               legacy_envelope=legacy_envelope)
    server = ThreadingHTTPServer(("127.0.0.1", 0), provider.handler())
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    (home / "config.toml").write_text(
        'model_provider = "bello_fixture"\ncli_auth_credentials_store = "file"\ncheck_for_update_on_startup = false\n'
        '[analytics]\nenabled = false\n[feedback]\nenabled = false\n'
        '[features]\nenable_request_compression = false\nremote_models = false\n'
        'plugins = false\nremote_plugin = false\nrecommended_plugins = false\napps = false\n'
        '[model_providers.bello_fixture]\nname = "Offline context fixture"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
        'request_max_retries = 0\nstream_max_retries = 0\n')
    env = isolated_environment(home, binary, server.server_port, {})
    session = ReceiptSession(binary, home, env)
    stage, error, status, resumed = "startup", None, None, False
    params = {"model": "gpt-5.5", "modelProvider": "bello_fixture", "cwd": str(workspace),
        "sandbox": "workspace-write", "approvalPolicy": "never", "ephemeral": False,
        "config": {"features.bello_context_compaction": enabled, "features.bello_async_tools": pending,
            "features.code_mode": False, "features.code_mode_only": False, "features.shell_zsh_fork": False,
            "model_reasoning_effort": "high", "model_post_turn_compact_threshold_percent": 0}}
    if enabled:
        params["config"].update(model_auto_compact_token_limit=THRESHOLD, bello_context_retain_percent=10)
    try:
        if os.name == "nt":
            await provision_windows_sandbox(binary, home, env, output)
            permissions = windows_permission_params(workspace, home, binary)
            params["config"].update(permissions.pop("config")); params.update(permissions); params.pop("sandbox")
        await session.start()
        features = await session.request("experimentalFeature/list", {"limit": 200})
        if "bello_context_compaction" not in {item.get("name") for item in features.get("data", [])}:
            raise ValueError("Missing context capability")
        reply = await session.request("thread/start", params)
        thread_id = reply["thread"]["id"]
        turn = await session.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": INITIAL, "text_elements": []}]})
        stage = "run"
        if not failure:
            async with asyncio.timeout(30):
                while not provider.second_requested.is_set():
                    await asyncio.sleep(0.01)
            await session.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn["turn"]["id"],
                "input": [{"type": "text", "text": CORRECTION, "text_elements": []}]})
            provider.allow_second.set()
        status = await session.terminal(thread_id)
        stage = "restart"
        await session.close(output)
        session = ReceiptSession(binary, home, env)
        await session.start()
        resume_params = {**params, "threadId": thread_id}
        resume_params.pop("ephemeral")
        if failure:
            resume_params["config"] = {**params["config"], "features.bello_context_compaction": False,
                                        "model_auto_compact_token_limit": 1000000}
        await session.request("thread/resume", resume_params)
        await session.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": INSPECT, "text_elements": []}]})
        resumed = await session.terminal(thread_id) == "completed"
    except Exception as exc:
        error = {"stage": stage, "type": type(exc).__name__}
    finally:
        provider.allow_second.set()
        await session.close(output)
        await asyncio.to_thread(server.shutdown)
        server.server_close(); worker.join(timeout=5)
    archive_present = any("ARCHIVED_RAW_ROUND_1_" in path.read_text(errors="replace") for path in home.rglob("rollout-*.jsonl"))
    execution_counts = [(workspace / f"execution-{number}.txt").read_text().count("x") if (workspace / f"execution-{number}.txt").exists() else 0
                        for number in range(1, provider.round + 1)]
    checks = evaluate(provider, enabled=enabled, failure=failure, status=status, resumed=resumed,
                      archive_present=archive_present, execution_counts=execution_counts)
    result = {"case": output.name, "passed": error is None and not provider.errors and not provider.rejected and all(checks.values()),
        "enabled": enabled, "summary_failure": failure, "pending_async": pending,
        "legacy_envelope": legacy_envelope, "checks": checks,
        "failure": error, "regular_requests": len(provider.regular), "summary_requests": len(provider.summaries),
        "turn_status": status, "resumed": resumed,
        "retained_source_rounds": sorted(source_markers(provider.regular[-1])) if provider.regular else [],
        "provider_errors": len(provider.errors), "blocked_external_requests": len(provider.rejected),
        "blocked_external_hosts": dict(Counter(urlsplit("//" + value.split(" ", 1)[1]).hostname
            if value.startswith("CONNECT ") else urlsplit(value.split(" ", 1)[1]).hostname for value in provider.rejected)),
        "paid_model_calls": 0, "native_stderr": getattr(session, "stderr_receipt", None)}
    (output / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


async def verify(binary, output, *, cases=None):
    from scripts.verify_native_codex_candidate import sha256
    binary_hash = sha256(binary)
    output.mkdir(parents=True, exist_ok=False)
    definitions = {"on": {}, "off": {"enabled": False}, "empty": {"failure": "empty"},
                   "truncated": {"failure": "truncated"}, "refused": {"failure": "refused"},
                   "oversized": {"failure": "oversized"}, "legacy": {"legacy_envelope": True},
                   "pending": {"pending": True}}
    selected = list(definitions) if cases is None else cases
    results = []
    for name in selected:
        result = await verify_case(binary, output / name, **definitions[name])
        results.append(result)
        print(json.dumps(result), flush=True)
    unchanged = sha256(binary) == binary_hash
    report = {"schema": "bello.native-context-smoke.v1", "paid_model_calls": 0,
              "binary_sha256": binary_hash, "binary_unchanged": unchanged,
              "passed": bool(results) and unchanged and all(item["passed"] for item in results), "results": results}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=("on", "off", "empty", "truncated", "refused", "oversized",
                                                       "legacy", "pending"))
    args = parser.parse_args()
    receipt = asyncio.run(verify(args.codex.absolute(), args.output_dir.absolute(), cases=args.cases))
    raise SystemExit(0 if receipt["passed"] else 1)
