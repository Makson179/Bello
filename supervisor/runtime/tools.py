"""One tool host for every model and execution engine.

Provider workers cannot select their own filesystem scope or approval policy.
They submit a call id and arguments against a host-created thread and turn.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import os
import secrets
from typing import Any

from jsonschema import Draft202012Validator

from supervisor.policy import is_secret_path
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.file_worker import parse_response
from supervisor.runtime.command_sessions import CommandCompletion, CommandSessionManager
from supervisor.runtime.output_budget import budget_output
from supervisor.runtime.sandbox import SandboxPolicy, SandboxRunner


TOOL_DEFINITIONS = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
_SCHEMAS = {entry["name"]: Draft202012Validator(entry["parameters"]) for entry in TOOL_DEFINITIONS}
_CHILD_TOOLS = frozenset({"spawn_agent", "send_message", "wait_agent", "close_agent"})

# These validate the trusted filesystem worker's transport, not model answers.
_FILE_RESULTS = {
    "read_file": {"text": str, "offset": int, "returned_lines": int, "total_lines": int},
    "search": {"matches": list, "errors": list},
    "list_directory": {"entries": list},
    "view_image": {"mimeType": str, "size": int, "data": str},
    "write_file": {"path": str, "bytes_written": int},
    "edit_file": {"path": str, "bytes_written": int},
}


@dataclass(frozen=True)
class ToolScope:
    root: Path
    mode: str
    readable_roots: tuple[Path, ...] = ()
    approval_policy: str = "on-request"
    network_access: bool = False


def tool_result(text: str, *, details: dict[str, Any] | None = None, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "details": details or {}, "isError": error}


class ToolHost:
    def __init__(
        self,
        journal: RuntimeJournal,
        scope_for: Callable[[str, str], ToolScope],
        approve: Callable[[str, dict[str, Any]], Awaitable[bool]],
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        delegate: Callable[[str, dict[str, Any], str, str], Awaitable[dict[str, Any]]],
        *,
        runner_factory: Callable[[SandboxPolicy], SandboxRunner] = SandboxRunner,
    ):
        self.journal, self.scope_for, self.approve, self.emit, self.delegate = journal, scope_for, approve, emit, delegate
        self.runner_factory = runner_factory
        self._active: dict[tuple[str, str], asyncio.Task] = {}
        self.sessions = CommandSessionManager(journal.directory / "commands")

    async def call(self, request: dict[str, Any]) -> dict[str, Any]:
        identifiers = [request.get(key) for key in ("threadId", "turnId", "callId", "name")]
        if not all(isinstance(value, str) and value for value in identifiers):
            raise ValueError("tool request requires threadId, turnId, callId and name")
        thread_id, turn_id, call_id, name = identifiers
        # Provider-local ids may legitimately repeat in a later conversation
        # turn. Scope both durable dispatch and controller item identity.
        call_id = "tool-" + hashlib.sha256(json.dumps([turn_id, call_id]).encode()).hexdigest()
        arguments = request.get("arguments")
        if name not in _SCHEMAS:
            raise ValueError(f"unknown tool: {name}")
        _SCHEMAS[name].validate(arguments)
        scope = self.scope_for(thread_id, turn_id)
        cached = self.journal.claim_tool(thread_id, call_id, name, {"turnId": turn_id, "arguments": arguments})
        if cached is not None:
            return cached
        task = asyncio.current_task()
        assert task is not None
        self._active[(thread_id, call_id)] = task
        try:
            if name in _CHILD_TOOLS:
                result = await self.delegate(name, arguments, thread_id, turn_id)
            elif name in {"poll_command", "stop_command"}:
                operation = self.sessions.poll if name == "poll_command" else self.sessions.stop
                snapshot = await operation(thread_id=thread_id, turn_id=turn_id, **arguments)
                result = self._command_packet(snapshot, stop_requested=name == "stop_command")
            else:
                result = await self._execute(name, arguments, scope, thread_id, turn_id, call_id)
            self.journal.complete_tool(thread_id, call_id, result)
            return result
        except asyncio.CancelledError:
            # Leave the durable claim uncertain. An already started command may
            # have made changes before cancellation and must never be replayed.
            raise
        except Exception as exc:
            result = tool_result(str(exc), error=True)
            self.journal.complete_tool(thread_id, call_id, result)
            return result
        finally:
            self._active.pop((thread_id, call_id), None)

    async def cancel_turn(self, thread_id: str) -> None:
        current = asyncio.current_task()
        tasks = [task for (owner, _), task in self._active.items() if owner == thread_id and task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.sessions.cancel_thread(thread_id)

    async def finish_turn(self, thread_id: str, turn_id: str) -> None:
        await self.sessions.cancel_turn(thread_id, turn_id)

    async def close(self) -> None:
        for thread_id in {owner for owner, _ in self._active}:
            await self.cancel_turn(thread_id)
        await self.sessions.close()

    @staticmethod
    def _command_packet(snapshot: dict[str, Any], *, stop_requested: bool = False) -> dict[str, Any]:
        # Tool details are not model-visible on every provider. Put the session
        # handle and exit status in content, without duplicating full output.
        packet = {key: value for key, value in snapshot.items() if key != "aggregatedOutput"}
        bounded = budget_output(packet.get("output", ""), mode="tail")
        packet["output"] = bounded.text
        packet["outputBudget"] = bounded.metadata
        return tool_result(json.dumps(packet, ensure_ascii=False), details=packet,
                           error=packet["status"] in {"failed", "timed_out", "lost"}
                           or packet["status"] == "cancelled" and not stop_requested
                           or bool(packet.get("callbackError")))

    def _path(self, scope: ToolScope, value: str, *, writing: bool = False) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = scope.root / path
        path = path.resolve()
        roots = (scope.root.resolve(),) if writing else (scope.root.resolve(), *scope.readable_roots)
        if scope.mode != "danger-full-access" and not any(path == root or path.is_relative_to(root) for root in roots):
            raise PermissionError("path is outside this agent's assigned workspace")
        if is_secret_path(path):
            raise PermissionError("access to secret material is not allowed")
        if writing and scope.mode == "read-only":
            raise PermissionError("this agent's workspace is read-only")
        return path

    async def _execute(self, name: str, args: dict[str, Any], scope: ToolScope,
                       thread_id: str, turn_id: str, call_id: str) -> dict[str, Any]:
        event_params = {"threadId": thread_id, "turnId": turn_id, "itemId": call_id}
        helper = Path(__file__).with_name("file_worker.py").resolve()
        cwd = self._path(scope, args.get("cwd", str(scope.root))) if name == "exec_command" else scope.root
        writing = name in {"write_file", "edit_file"}
        path = self._path(scope, args["path"], writing=writing) if "path" in args else None
        if name == "exec_command":
            command = args["command"]
            item = {"id": call_id, "type": "commandExecution", "command": command, "cwd": str(cwd), "status": "inProgress"}
            escalation = args.get("sandbox_permissions") == "require_escalated"
            approval_method = "item/commandExecution/requestApproval" if escalation else None
            approval = {**event_params, "command": command, "cwd": str(cwd),
                        "reason": args.get("justification", "This command requests execution outside its sandbox."),
                        "availableDecisions": ["accept", "decline"]}
        else:
            response_nonce = secrets.token_hex(16)
            operation = {"name": name, "arguments": {**args, "path": str(path)}, "response_nonce": response_nonce}
            encoded = base64.b64encode(json.dumps(operation).encode()).decode()
            argv = [str(Path(sys.executable).resolve()), "-I", str(helper), encoded]
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            item = {"id": call_id, "type": "fileChange" if writing else "fileRead", "status": "inProgress",
                    "tool": name, "arguments": args, "cwd": str(cwd), "paths": [str(path)]}
            if writing:
                item["changes"] = [{"path": str(path), "kind": {"type": "update"}}]
            escalation = False
            # Contained writes were already permitted by the original Codex
            # sandbox. Only an explicit escape asks the approval gate to grant
            # more access; normal tools do not add a paid reviewer round trip.
            approval_method = None
            approval = {**event_params, "cwd": str(cwd), "changes": item.get("changes", []), "availableDecisions": ["accept", "decline"]}
        await self.emit({"method": "item/started", "params": {**event_params, "item": dict(item)}})
        completed = False
        yielded = False
        try:
            if approval_method:
                if scope.approval_policy == "never" or not await self.approve(
                    approval_method, {**approval, "belloApprovalPolicy": scope.approval_policy}
                ):
                    item.update(status="declined", exitCode=None)
                    return tool_result("Bello denied this action. No command was executed.", error=True)
            # Scope may have been cancelled/revised while approval was pending.
            self.scope_for(thread_id, turn_id)
            runner = self.runner_factory(SandboxPolicy(
                # This grant applies to this exact invocation only. It never
                # changes the thread, its children, or later tool calls.
                root=scope.root, mode="danger-full-access" if escalation else scope.mode,
                network_access=scope.network_access,
                readable_roots=(*scope.readable_roots, helper),
            ))

            async def output_delta(chunk: str) -> None:
                if name == "exec_command":
                    await self.emit({"method": "item/commandExecution/outputDelta", "params": {**event_params, "delta": chunk}})

            if name == "exec_command":
                async def finished(completion: CommandCompletion) -> None:
                    nonlocal completed
                    result = completion.result
                    if result is not None:
                        item.update(status="interrupted" if result.cancelled else "completed" if result.exit_code == 0 else "failed",
                                    exitCode=result.exit_code, aggregatedOutput=result.output, timedOut=result.timed_out,
                                    durationMs=round(result.duration * 1000))
                    else:
                        item.update(status="interrupted" if completion.status == "cancelled" else "failed",
                                    error=str(completion.error), exitCode=None)
                    completed = True
                    await self.emit({"method": "item/completed", "params": {**event_params, "item": dict(item)}})

                snapshot = await self.sessions.start(thread_id=thread_id, turn_id=turn_id, call_id=call_id,
                    runner=runner, command=command, cwd=cwd, timeout=args.get("timeout", 120),
                    yield_time_ms=args.get("yield_time_ms", 10_000), on_output=output_delta, on_finished=finished)
                yielded = True
                return self._command_packet(snapshot)

            result = await runner.run(command, cwd, args.get("timeout", 120), on_output=output_delta)
            # Preserve the complete transport for diagnostics even if parsing fails.
            item.update(exitCode=result.exit_code, aggregatedOutput=result.output,
                        durationMs=round(result.duration * 1000), timedOut=result.timed_out)
            data = None
            diagnostics = ""
            output = result.output
            if result.exit_code == 0:
                data, diagnostics = parse_response(result.output, response_nonce, name)
                fields = _FILE_RESULTS[name]
                if set(data) != set(fields) or any(type(data[key]) is not kind for key, kind in fields.items()):
                    raise ValueError("file-worker response has invalid result fields")
                output = json.dumps(data, ensure_ascii=False)
                # Keep stderr outside the result, without silently discarding it.
                item.update(aggregatedOutput=output, diagnostics=diagnostics)
            else:
                # A normal worker error has the same frame. If the interpreter
                # itself failed before producing it, retain the original output.
                try:
                    error_data, error_diagnostics = parse_response(result.output, response_nonce, name)
                except ValueError:
                    pass
                else:
                    if set(error_data) == {"error"} and isinstance(error_data["error"], str):
                        output = json.dumps(error_data, ensure_ascii=False)
                        diagnostics = error_diagnostics
                        item.update(aggregatedOutput=output, diagnostics=diagnostics)
            if name == "view_image" and result.exit_code == 0:
                image = data
                summary = f"Opened {path.name}: {image['mimeType']}, {image['size']} bytes."
                item.update(status="completed", exitCode=0, aggregatedOutput=summary,
                            durationMs=round(result.duration * 1000))
                completed = True
                return {"content": [{"type": "text", "text": summary},
                                    {"type": "image", "mimeType": image["mimeType"], "data": image["data"]}],
                        "details": {"path": str(path), "diagnostics": budget_output(diagnostics, mode="tail").text}, "isError": False}
            item.update(status="completed" if result.exit_code == 0 else "failed", exitCode=result.exit_code,
                        aggregatedOutput=output, durationMs=round(result.duration * 1000), timedOut=result.timed_out)
            completed = True
            offset = None
            continuation = ""
            if result.exit_code == 0 and name in {"read_file", "search", "list_directory"}:
                if name == "read_file":
                    output = data["text"]
                    offset = data["offset"]
                    if offset + data["returned_lines"] - 1 < data["total_lines"]:
                        continuation = f"\n[bello: file has {data['total_lines']} lines; continue with offset={offset + data['returned_lines']}.]"
                elif name == "search":
                    output = (f"Matches: {len(data['matches'])}; unreadable files: {len(data['errors'])}\n"
                              + "\n".join(json.dumps(entry, ensure_ascii=False) for entry in [*data["matches"], *data["errors"]]))
                else:
                    output = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in data["entries"])
            bounded = budget_output(output, mode="head", offset=offset)
            return tool_result(bounded.text + (continuation if not bounded.metadata["truncated"] else ""),
                               details={"exitCode": result.exit_code, "duration": result.duration,
                                        "timedOut": result.timed_out, "outputBudget": bounded.metadata,
                                        "diagnostics": budget_output(diagnostics, mode="tail").text},
                               error=result.exit_code != 0)
        except asyncio.CancelledError:
            item["status"] = "interrupted"
            raise
        except Exception as exc:
            item.update(status="failed", error=str(exc))
            raise
        finally:
            if not completed and item["status"] == "inProgress":
                item["status"] = "failed"
            if not yielded and not (name == "exec_command" and completed):
                await self.emit({"method": "item/completed", "params": {**event_params, "item": item}})
