#!/usr/bin/env python3
"""Credential-free proof of native Async tools at the provider boundary.

Runs real native commands against a localhost scripted Responses provider. It
checks ON/OFF, no empty polling with ON, late-output delivery, and unchanged
previous prompt prefixes. No subscription credentials or paid model are used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_native_codex_selection import (
    CASES, LIMIT, NativeSession, decode_body, isolated_environment, provision_windows_sandbox,
    run_case as verify_selection_case,
    windows_permission_params, windows_shell,
)


def tool(call_id, name, arguments, *, custom=False):
    return {"type": "custom_tool_call" if custom else "function_call", "id": "item-" + call_id,
            "call_id": call_id, "name": name, **({"input": arguments} if custom else {"arguments": json.dumps(arguments)})}


def outputs(request):
    return [item for item in request.get("input", [])
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}
            or (item.get("type") == "message" and item.get("role") == "user"
                and any(part.get("text", "").startswith("<bello_async_tool_result>")
                        for part in item.get("content", []) if isinstance(part, dict)))]


def output_text(item):
    value = item.get("output", item.get("content", ""))
    return value if isinstance(value, str) else "\n".join(part.get("text", "") for part in value if isinstance(part, dict))


def delivered(request, marker):
    """Count actual successful terminal outputs, not command echoes in errors."""
    count = 0
    for item in outputs(request):
        value = output_text(item)
        prefix, separator, payload = value.partition("\nOutput:\n")
        if separator and re.search(r"(?m)^Process exited with code 0$", prefix) and marker in payload:
            count += 1
            continue
        decoder, offset = json.JSONDecoder(), 0
        while (start := value.find("{", offset)) >= 0:
            try:
                decoded, consumed = decoder.raw_decode(value[start:])
            except ValueError:
                offset = start + 1
                continue
            offset = start + consumed
            if isinstance(decoded, dict) and decoded.get("exit_code") == 0 and marker in decoded.get("output", ""):
                count += 1
                break
    return count


def unique_tool_resolutions(request):
    ids = [item.get("call_id") for item in request.get("input", [])
           if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
    return len(ids) == len(set(ids))


class ScriptedProvider:
    def __init__(self, *, enabled, code, child_wait=False, interrupt_probe=False):
        self.enabled, self.code = enabled, code
        self.child_wait = child_wait
        self.interrupt_probe = interrupt_probe
        self.slow_seconds = 12 if os.name == "nt" else 2.5
        self.requests, self.times, self.errors, self.tools = [], [], [], []
        self.rejected = []

    def response(self, request):
        number = len(self.requests)
        if number == 1:
            command = f"sleep {self.slow_seconds}; printf ASYNC_SLOW_DONE"
            fast_command = "printf ASYNC_FAST_DONE"
            if os.name == "nt":
                command = f"Start-Sleep -Seconds {self.slow_seconds}; [Console]::Out.Write('ASYNC_SLOW_DONE')"
                fast_command = "[Console]::Out.Write('ASYNC_FAST_DONE')"
            if self.interrupt_probe:
                if os.name == "nt":
                    child = (
                        "[IO.File]::WriteAllText((Join-Path (Get-Location).Path 'cancel.started'), 'started'); "
                        f"Start-Sleep -Seconds {self.slow_seconds}; "
                        "[IO.File]::WriteAllText((Join-Path (Get-Location).Path 'cancel.orphan'), 'orphan')")
                    command = ("& (Join-Path $PSHOME 'powershell.exe') -NoProfile -NonInteractive "
                               f'-Command "{child}"; [Console]::Out.Write(\'ASYNC_SLOW_DONE\')')
                else:
                    command = (f"/bin/sh -c 'printf started > cancel.started; sleep {self.slow_seconds}; "
                               "printf orphan > cancel.orphan'; printf ASYNC_SLOW_DONE")
            args = {"cmd": command, "yield_time_ms": 1, "max_output_tokens": 200, "login": False}
            shell = {"shell": windows_shell()} if os.name == "nt" else {}
            args.update(shell)
            if self.code:
                args["yield_time_ms"] = 30_000
                source = "// @exec: {\"yield_time_ms\": 1}\ntext(await tools.exec_command(" + json.dumps(args) + "));"
                items = [tool("slow", "exec", source, custom=True)]
            else:
                slow = tool("slow", "bello_wait_agent", {}) if self.child_wait else tool("slow", "exec_command", args)
                items = [slow, tool("fast", "exec_command", {
                    "cmd": fast_command, "yield_time_ms": 1, "max_output_tokens": 200, "login": False, **shell})]
        else:
            packets = outputs(request)
            latest = output_text(packets[-1]) if packets else ""
            done = delivered(request, "ASYNC_SLOW_DONE") == 1
            if done or self.enabled:
                items = [{"type": "message", "id": f"msg-{number}", "role": "assistant",
                          "content": [{"type": "output_text", "text": "Done." if done else "Waiting for the remaining result."}]}]
            elif self.code and (match := re.search(r"Script running with cell ID ([^\s]+)", latest)):
                items = [tool(f"poll-{number}", "wait", {"cell_id": match[1], "yield_time_ms": 1000 if os.name == "nt" else 100})]
            else:
                process_ids = [match[1] for item in packets
                               if (match := re.search(r"Process running with session ID (\d+)", output_text(item)))]
                if not process_ids:
                    raise ValueError("OFF fixture could not find pending native process/cell")
                items = [tool(f"poll-{number}", "write_stdin", {"session_id": int(process_ids[0]), "chars": "", "yield_time_ms": 1000 if os.name == "nt" else 100})]
        self.tools.extend(item.get("name") for item in items if "name" in item)
        response_id = f"response-{number}"
        events = [{"type": "response.created", "response": {"id": response_id}}]
        events += [{"type": "response.output_item.done", "item": item} for item in items]
        events += [{"type": "response.completed", "response": {"id": response_id, "usage": {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]
        return "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()

    def handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def reject(self):
                owner.rejected.append(self.command + " " + self.path)
                self.send_error(403)
            do_GET = do_CONNECT = do_PUT = reject
            def do_POST(self):
                try:
                    if self.path != "/v1/responses" or self.headers.get("Authorization") or self.headers.get("Cookie"):
                        raise ValueError("Unexpected endpoint or credentials")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= LIMIT:
                        raise ValueError("Invalid request size")
                    request = decode_body(self.rfile.read(length), self.headers.get("Content-Encoding", ""))
                    owner.requests.append(request)
                    owner.times.append(time.monotonic())
                    if len(owner.requests) > 40:
                        raise ValueError("Too many fixture inference requests")
                    body = owner.response(request)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    owner.errors.append(str(error))
                    self.send_error(500)
        return Handler


class ChildWaitSession(NativeSession):
    """Only the deliberately registered synthetic child wait is permitted."""
    def __init__(self, *args, wait_seconds):
        super().__init__(*args)
        self.wait_seconds, self.waiters = wait_seconds, set()

    async def server_request(self, message):
        if message.get("method") != "item/tool/call" or message.get("params", {}).get("tool") != "bello_wait_agent":
            return await super().server_request(message)

        async def finish():
            await asyncio.sleep(self.wait_seconds)
            await self.send({"id": message["id"], "result": {"success": True,
                "contentItems": [{"type": "inputText", "text": json.dumps({"exit_code": 0, "output": "ASYNC_SLOW_DONE"})}]}})
        task = asyncio.create_task(finish())
        self.waiters.add(task)
        task.add_done_callback(self.waiters.discard)

    async def close(self, output):
        tasks = list(self.waiters)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close(output)


async def verify_case(binary: Path, output: Path, *, enabled: bool, code: bool, signal=None, child_wait=False):
    output.mkdir(parents=True, exist_ok=False)
    home, workspace = output / "empty-home", output / "work"
    home.mkdir()
    workspace.mkdir()
    provider = ScriptedProvider(enabled=enabled, code=code, child_wait=child_wait,
                                interrupt_probe=signal == "interrupt")
    server = ThreadingHTTPServer(("127.0.0.1", 0), provider.handler())
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    port = server.server_port
    (home / "config.toml").write_text(
        'model_provider = "bello_fixture"\ncli_auth_credentials_store = "file"\n'
        '[features]\nenable_request_compression = false\n'
        '[model_providers.bello_fixture]\nname = "Offline fixture"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
        'request_max_retries = 0\nstream_max_retries = 0\n')
    session = ChildWaitSession(binary, home, isolated_environment(home, binary, port, {}), wait_seconds=provider.slow_seconds)
    failure = None
    interrupted = False
    interrupted_command_started = False
    requests_at_interrupt = None
    try:
        if os.name == "nt":
            await provision_windows_sandbox(binary, home, session.env, output)
        await session.start()
        features = await session.request("experimentalFeature/list", {"limit": 100})
        names = {item.get("name") for item in features.get("data", [])}
        if not {"bello_async_tools", "bello_native_selection"} <= names:
            raise ValueError("Native binary does not advertise both Bello capabilities")
        params = {
            "model": "gpt-6-astra" if code else "gpt-5.5", "modelProvider": "bello_fixture",
            "cwd": str(workspace), "sandbox": "workspace-write" if signal == "interrupt" else "read-only",
            "approvalPolicy": "never", "ephemeral": True,
            "config": {"features.bello_async_tools": enabled, "features.code_mode": code,
                       "features.code_mode_only": code, "features.shell_zsh_fork": False}}
        if os.name == "nt":
            permissions = windows_permission_params(workspace, home, binary)
            params["config"].update(permissions.pop("config"))
            params.update(permissions)
            params.pop("sandbox")
        if child_wait:
            params["dynamicTools"] = [{"name": "bello_wait_agent", "description": "Wait for the synthetic child.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
        reply = await session.request("thread/start", params)
        started = await session.request("turn/start", {"threadId": reply["thread"]["id"],
            "input": [{"type": "text", "text": "Run only the synthetic local fixture.", "text_elements": []}]})
        if signal:
            async with asyncio.timeout(20):
                while not provider.requests:
                    await asyncio.sleep(0.01)
            if signal == "steer":
                await asyncio.sleep(0.15)
                await session.request("turn/steer", {"threadId": reply["thread"]["id"],
                    "expectedTurnId": started["turn"]["id"],
                    "input": [{"type": "text", "text": "Acknowledge this steer while the command is running.", "text_elements": []}]})
            else:
                # Observe a real allowed workspace write before cancellation,
                # rather than cancelling while native sandbox setup still runs.
                async with asyncio.timeout(20):
                    while not (workspace / "cancel.started").is_file():
                        await asyncio.sleep(0.01)
                interrupted_command_started = True
                await session.request("turn/interrupt", {"threadId": reply["thread"]["id"], "turnId": started["turn"]["id"]})
                async with asyncio.timeout(5):
                    while True:
                        event = await session.notifications.get()
                        if event.get("method") == "turn/completed":
                            interrupted = event["params"]["turn"]["status"] == "interrupted"
                            break
                requests_at_interrupt = len(provider.requests)
                # Keep the session alive beyond the original command lifetime:
                # cancellation must stop both model continuations and the OS
                # command's delayed write, even while this session stays alive.
                await asyncio.sleep(provider.slow_seconds + 0.2)
        if signal != "interrupt":
            await session.complete(reply["thread"]["id"], timeout=60)
    except Exception as error:
        failure = str(error)
    finally:
        await session.close(output)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=5)
    for index, request in enumerate(provider.requests, 1):
        (output / f"provider-request-{index}.json").write_text(json.dumps(request, indent=2) + "\n")
    terminal = bool(provider.requests and delivered(provider.requests[-1], "ASYNC_SLOW_DONE") == 1)
    fast_delivered = code or bool(provider.requests and delivered(provider.requests[-1], "ASYNC_FAST_DONE") == 1)
    unique_resolutions = all(unique_tool_resolutions(request) for request in provider.requests)
    expected_requests = 2 if code else 3
    prefix_stable = all(right.get("input", [])[:len(left.get("input", []))] == left.get("input", [])
                        for left, right in zip(provider.requests[1:], provider.requests[2:]))
    no_polls = not ({"wait", "write_stdin"} & set(provider.tools))
    instructions = provider.requests[0].get("instructions", "") if provider.requests else ""
    if not instructions and provider.requests:
        # Code Mode places the unchanged model-specific base prompt in its
        # first developer message rather than the legacy instructions field.
        instructions = next(("\n".join(part.get("text", "") for part in item.get("content", []))
                             for item in provider.requests[0].get("input", [])
                             if item.get("type") == "message" and item.get("role") == "developer"
                             and any(part.get("text", "").startswith("You are Codex")
                                     for part in item.get("content", []))), "")
    native_instructions = isinstance(instructions, str) and len(instructions) > 1000
    passed = failure is None and not provider.errors and terminal and fast_delivered and native_instructions and unique_resolutions
    if signal == "interrupt":
        passed = (failure is None and not provider.errors and interrupted and interrupted_command_started
                  and not (workspace / "cancel.orphan").exists()
                  and requests_at_interrupt in (1, 2) and len(provider.requests) == requests_at_interrupt)
    elif enabled:
        passed &= no_polls and len(provider.requests) == expected_requests and prefix_stable
        if not code and len(provider.times) == 3:
            passed &= (0.1 <= provider.times[1] - provider.times[0] < 0.8) if signal == "steer" else (
                0.8 <= provider.times[1] - provider.times[0] < provider.slow_seconds - 0.1)
            passed &= provider.times[2] - provider.times[0] >= provider.slow_seconds - 0.1
    else:
        passed &= not no_polls
    return {"case": output.name, "enabled": enabled, "code_mode": code, "signal": signal,
            "interrupted": interrupted, "child_wait": child_wait,
            "passed": bool(passed), "failure": failure,
            "provider_errors": provider.errors, "provider_requests": len(provider.requests),
            "rejected_network_requests": provider.rejected, "external_proxy_requests_forwarded": 0,
            "tools": provider.tools, "prefix_stable": prefix_stable, "terminal_output_delivered": terminal,
            "fast_output_delivered": fast_delivered, "unique_tool_resolutions": unique_resolutions,
            "native_instructions_preserved": native_instructions,
            "native_instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
            "interrupted_command_started": interrupted_command_started,
            "cancelled_command_late_write_absent": not (workspace / "cancel.orphan").exists() if signal == "interrupt" else None,
            "provider_requests_at_interrupt": requests_at_interrupt,
            "request_offsets": [round(value - provider.times[0], 3) for value in provider.times]}


async def verify(binary, output, *, concurrency_repeats=1):
    if not 1 <= concurrency_repeats <= 10:
        raise ValueError("concurrency_repeats must be between 1 and 10")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for code in (False, True):
        for enabled in (False, True):
            name = ("code" if code else "direct") + ("_on" if enabled else "_off")
            result = await verify_case(binary, output / name, enabled=enabled, code=code)
            results.append(result)
            print(json.dumps({"case": name, **result}), flush=True)
    for signal in ("steer", "interrupt"):
        result = await verify_case(binary, output / signal, enabled=True, code=False, signal=signal)
        results.append(result)
        print(json.dumps({"case": signal, **result}), flush=True)
    result = await verify_case(binary, output / "child_wait", enabled=True, code=False, child_wait=True)
    results.append(result)
    print(json.dumps({"case": "child_wait", **result}), flush=True)
    for case in (item for item in CASES if item.name in {"direct_on", "code_on"}):
        name = "async_and_selection_" + case.mode
        result = {**await verify_selection_case(binary, case, output / name, async_tools=True),
                  "case": name, "async_tools": True}
        results.append(result)
        print(json.dumps(result), flush=True)
    # Fresh homes exercise concurrent first-use setup, not merely an already
    # warmed sandbox. Keep every attempt: a later success cannot hide a race.
    for repeat in range(2, concurrency_repeats + 1):
        for signal in (None, "steer"):
            name = f"{'steer' if signal else 'direct_on'}_repeat_{repeat}"
            result = await verify_case(binary, output / name, enabled=True, code=False, signal=signal)
            results.append(result)
            print(json.dumps({"case": name, **result}), flush=True)
    unchanged_instructions = all(results[index]["native_instructions_sha256"] == results[index + 1]["native_instructions_sha256"]
                                 for index in (0, 2))
    report = {"schema": "bello.native-async-smoke.v1", "paid_model_calls": 0,
              "passed": all(result["passed"] for result in results) and unchanged_instructions,
              "on_off_native_instructions_identical": unchanged_instructions, "results": results}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--concurrency-repeats", type=int, choices=range(1, 11), default=1,
                        help="Repeat direct ON and steering with separate cold sandbox homes")
    args = parser.parse_args()
    result = asyncio.run(verify(args.codex.absolute(), args.output_dir.absolute(),
                               concurrency_repeats=args.concurrency_repeats))
    raise SystemExit(0 if result["passed"] else 1)
