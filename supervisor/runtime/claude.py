"""Claude Code subscription backend built on Anthropic's official Agent SDK.

The CLI process is an execution engine, not a second policy boundary.  It has
no built-in tools and receives only Bello's in-process MCP server.  Every tool
call therefore returns to :class:`ToolHost`, which owns approvals, workspace
scope, cancellation and at-most-once dispatch.

Authentication is deliberately narrower than the Agent SDK supports.  This
backend accepts only an existing first-party ``claude.ai`` subscription login
from the unmodified CLI bundled with ``claude-agent-sdk``.  It never reads,
copies, refreshes or persists credentials, and it refuses API-key and hosted
cloud-provider routes instead of silently changing the caller's environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from supervisor.appserver import AppServerError, AppServerTimeoutError
from supervisor.runtime.models import validate_effort


SUPPORTED_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MCP_SERVER = "bello"
_STATE_VERSION = 1
_SYSTEM_PROMPT = (
    "You are a coding agent running inside Bello. Use only the provided Bello MCP tools "
    "for filesystem access, commands, delegation, and communication. Bello owns workspace "
    "scope, approvals, cancellation, and child-agent policy. Do not attempt to discover or "
    "invoke ambient Claude Code tools, hooks, skills, plugins, agents, MCP servers, memory, "
    "or project instruction files."
)
_DISALLOWED_BUILTINS = [
    "Agent",
    "Task",
    "TaskOutput",
    "TaskStop",
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
    "TodoWrite",
    "AskUserQuestion",
    "Skill",
    "EnterPlanMode",
    "ExitPlanMode",
]

# A non-empty value for any of these can replace the user's normal Claude Code
# login.  Names, never values, are included in diagnostics.
_DIRECT_CREDENTIAL_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FEDERATION_RULE_ID",
    }
)
_PROVIDER_SWITCH_ENV = frozenset(
    {
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    }
)
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,255}\Z")
_TOOL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MCP_REQUEST_CONTEXT: ContextVar[Any | None] = ContextVar(
    "bello_claude_mcp_request_context", default=None
)


class _CaptureMcpRequestContext:
    """Expose the MCP 2.x request context to the SDK's args-only tool handler."""

    async def __call__(self, context: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        token = _MCP_REQUEST_CONTEXT.set(context)
        try:
            return await call_next(context)
        finally:
            _MCP_REQUEST_CONTEXT.reset(token)


@dataclass
class _Command:
    kind: str
    params: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


@dataclass
class _Owner:
    queue: asyncio.Queue[_Command]
    task: asyncio.Task[None]


class ClaudeBackend:
    """Normalized thread/turn API over a first-party Claude Code session."""

    def __init__(
        self,
        state_dir: Path,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        tool_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        client_factory: Callable[[Any], Any] | None = None,
        auth_probe: Callable[[], Any] | None = None,
        environment: Mapping[str, str] | None = None,
        cli_path: Path | None = None,
    ):
        self.state_dir = Path(state_dir).absolute()
        self.emit = emit
        self.tool_handler = tool_handler
        self._environment = dict(os.environ if environment is None else environment)
        self._client_factory = client_factory
        self._auth_probe = auth_probe
        self._cli_path_override = Path(cli_path).absolute() if cli_path is not None else None
        self._cli_path: Path | None = None
        self._auth: dict[str, Any] | None = None
        self._catalog: list[dict[str, Any]] | None = None
        self._threads: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, _Owner] = {}
        self._closing = False
        self._state_file = self.state_dir / "threads.json"
        if environment is not None and client_factory is None:
            raise AppServerError(
                "an alternate environment is test-only and requires an injected Claude SDK client"
            )
        if cli_path is not None and client_factory is None:
            raise AppServerError(
                "Claude Code production mode always uses the official CLI bundled with claude-agent-sdk"
            )
        self._prepare_state_directory()
        self._load_state()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Handle one normalized request without retrying uncertain actions."""
        if timeout <= 0:
            raise AppServerTimeoutError(f"Claude Code {method} timed out before dispatch")
        try:
            async with asyncio.timeout(timeout):
                return await self._dispatch(method, deepcopy(params or {}))
        except TimeoutError as exc:
            raise AppServerTimeoutError(
                f"Claude Code {method} timed out; an uncertain action is not retried"
            ) from exc

    async def stop(self) -> None:
        """Stop live SDK owners while leaving resumable thread metadata intact."""
        self._closing = True
        owners = list(self._owners.items())
        for thread_id, owner in owners:
            if owner.task.done():
                continue
            future = asyncio.get_running_loop().create_future()
            await owner.queue.put(_Command("stop", {"threadId": thread_id}, future))
        for thread_id, owner in owners:
            if owner.task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(owner.task), 10)
            except asyncio.TimeoutError:
                owner.task.cancel()
                await asyncio.gather(owner.task, return_exceptions=True)
        self._owners.clear()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return await self._initialize()
        if method == "account/read":
            await self._ensure_initialized()
            assert self._auth is not None
            return {
                "provider": "claude-code",
                "billingRoute": "subscription",
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": self._auth["subscriptionType"],
            }
        if method == "model/list":
            await self._ensure_initialized()
            if self._catalog is None:
                self._catalog = await self._read_model_catalog()
            return {"data": deepcopy(self._catalog)}
        if method == "model/validate":
            return await self._model_validate(params)
        if method == "thread/start":
            return await self._thread_start(params)
        if method == "thread/resume":
            return await self._thread_resume(params)
        if method == "thread/read":
            record = self._record(params.get("threadId"))
            return {"thread": self._public_thread(record, include_turns=bool(params.get("includeTurns", True)))}
        if method == "thread/list":
            return self._thread_list(params)
        if method == "thread/turns/list":
            return self._turns_list(params)
        if method in {"thread/archive", "thread/unsubscribe"}:
            return await self._thread_close(params)
        if method == "turn/start":
            return await self._turn_start(params)
        if method == "turn/steer":
            return await self._turn_steer(params)
        if method == "turn/interrupt":
            return await self._turn_interrupt(params)
        raise AppServerError(f"Claude Code backend does not support {method}")

    async def _initialize(self) -> dict[str, Any]:
        self._assert_subscription_environment()
        self._cli_path = self._cli_path_override or self._bundled_cli_path()
        raw = await self._run_auth_probe()
        if raw.get("loggedIn") is not True:
            raise AppServerError(
                "Claude Code is not signed in. Run `claude` interactively and sign in with the "
                "Claude subscription account before starting Bello. `claude -p` cannot perform login."
            )
        if raw.get("apiProvider") != "firstParty" or raw.get("authMethod") != "claude.ai":
            raise AppServerError(
                "Bello's claude-code route requires an existing first-party claude.ai login; "
                "API keys and Bedrock, Vertex, Foundry or gateway credentials are not accepted"
            )
        subscription = raw.get("subscriptionType")
        if not isinstance(subscription, str) or not subscription.strip() or subscription.lower() == "free":
            raise AppServerError(
                "Claude Code is signed in, but no paid Claude subscription was reported by the official CLI"
            )
        self._auth = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": subscription,
        }
        return {
            "engine": "claude-code",
            "protocolVersion": 1,
            "billingRoute": "subscription",
            "auth": deepcopy(self._auth),
            "capabilities": {
                "threads": True,
                "steering": True,
                "interrupt": True,
                "structuredOutput": True,
                "builtinTools": False,
                "supportedEfforts": list(SUPPORTED_EFFORTS),
            },
        }

    async def _ensure_initialized(self) -> None:
        if self._auth is None:
            await self._initialize()

    async def _thread_start(self, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_initialized()
        if self._closing:
            raise AppServerError("Claude Code backend is stopping")
        thread_id = self._identifier(params.get("threadId"), "threadId")
        if thread_id in self._threads:
            raise AppServerError("duplicate Claude Code thread id")
        if params.get("provider") not in (None, "claude-code"):
            raise AppServerError("Claude Code backend accepts only provider claude-code")
        model = self._model(params.get("model"))
        cwd = Path(params.get("cwd", "")).absolute()
        if not cwd.is_dir():
            raise AppServerError("Claude Code thread cwd must be an existing directory")
        tools = self._validate_tools(params.get("tools", []))
        developer = params.get("developerInstructions")
        if developer is not None and not isinstance(developer, str):
            raise AppServerError("developerInstructions must be text")
        effort = params.get("effort")
        self._validate_effort(effort, SUPPORTED_EFFORTS)
        record: dict[str, Any] = {
            "id": thread_id,
            "model": model,
            "cwd": str(cwd),
            "tools": tools,
            "developerInstructions": developer or "",
            "claudeSessionId": str(uuid4()),
            "hasSession": False,
            "effort": effort,
            "turns": [],
            "closed": False,
        }
        for key in ("parentThreadId", "ephemeral"):
            if key in params:
                record[key] = deepcopy(params[key])
        self._threads[thread_id] = record
        self._persist()
        await self._ensure_owner(record)
        thread = self._public_thread(record, include_turns=False)
        await self.emit(
            {"method": "thread/started", "params": {"threadId": thread_id, "thread": deepcopy(thread)}}
        )
        return {"thread": thread}

    async def _thread_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_initialized()
        record = self._record(params.get("threadId"))
        if "model" in params and self._model(params["model"]) != record["model"]:
            raise AppServerError("resuming a Claude Code thread cannot change its model")
        if "tools" in params:
            tools = self._validate_tools(params["tools"])
            if tools != record["tools"]:
                raise AppServerError("resuming a Claude Code thread cannot change its tool contract")
        record["closed"] = False
        record.pop("activeTurnId", None)
        self._persist()
        await self._ensure_owner(record)
        return {"thread": self._public_thread(record, include_turns=True)}

    async def _thread_close(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(params.get("threadId"))
        owner = self._owners.get(record["id"])
        if owner and not owner.task.done():
            await self._send_owner(record["id"], "stop", {"threadId": record["id"]})
            await asyncio.gather(owner.task, return_exceptions=True)
        record["closed"] = True
        record.pop("activeTurnId", None)
        self._persist()
        return {"thread": self._public_thread(record, include_turns=False)}

    async def _turn_start(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(params.get("threadId"))
        if record.get("closed"):
            raise AppServerError("cannot start a turn on an archived Claude Code thread")
        if record.get("activeTurnId"):
            raise AppServerError("Claude Code thread already has an active turn")
        turn_id = self._identifier(params.get("turnId"), "turnId")
        if any(turn.get("id") == turn_id for turn in record["turns"]):
            raise AppServerError("duplicate Claude Code turn id")
        prompt = self._input_text(params.get("input"))
        effort = params.get("effort", record.get("effort"))
        self._validate_effort(effort, SUPPORTED_EFFORTS)
        schema = params.get("outputSchema")
        if schema is not None:
            if not isinstance(schema, dict):
                raise AppServerError("outputSchema must be a JSON Schema object")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise AppServerError("outputSchema is not a valid Draft 2020-12 JSON Schema") from exc
        turn = {"id": turn_id, "status": "inProgress", "items": []}
        record["turns"].append(turn)
        record["activeTurnId"] = turn_id
        record["effort"] = effort
        self._persist()
        await self._ensure_owner(record)
        await self.emit(
            {
                "method": "turn/started",
                "params": {"threadId": record["id"], "turnId": turn_id, "turn": deepcopy(turn)},
            }
        )
        await self._send_owner(
            record["id"],
            "start",
            {"turnId": turn_id, "prompt": prompt, "effort": effort, "outputSchema": deepcopy(schema)},
        )
        return {"turn": deepcopy(turn)}

    async def _turn_steer(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(params.get("threadId"))
        expected = self._identifier(params.get("expectedTurnId"), "expectedTurnId")
        if record.get("activeTurnId") != expected:
            raise AppServerError("cannot steer an inactive or stale Claude Code turn")
        prompt = self._input_text(params.get("input"))
        return await self._send_owner(
            record["id"], "steer", {"turnId": expected, "prompt": prompt}
        )

    async def _turn_interrupt(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(params.get("threadId"))
        turn_id = self._identifier(params.get("turnId"), "turnId")
        if record.get("activeTurnId") != turn_id:
            return {}
        return await self._send_owner(record["id"], "interrupt", {"turnId": turn_id})

    async def _ensure_owner(self, record: dict[str, Any]) -> None:
        owner = self._owners.get(record["id"])
        if owner and not owner.task.done():
            return
        queue: asyncio.Queue[_Command] = asyncio.Queue()
        task = asyncio.create_task(self._owner_loop(record["id"], queue))
        self._owners[record["id"]] = _Owner(queue, task)

    async def _send_owner(self, thread_id: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        owner = self._owners.get(thread_id)
        if owner is None or owner.task.done():
            if owner and owner.task.done() and not owner.task.cancelled():
                error = owner.task.exception()
                if error is not None:
                    raise AppServerError("Claude Code session owner stopped unexpectedly") from error
            record = self._record(thread_id)
            await self._ensure_owner(record)
            owner = self._owners[thread_id]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await owner.queue.put(_Command(kind, params, future))
        return await future

    async def _owner_loop(self, thread_id: str, queue: asyncio.Queue[_Command]) -> None:
        try:
            while True:
                command = await queue.get()
                if command.kind == "stop":
                    self._resolve(command, {})
                    return
                if command.kind == "interrupt":
                    # Completion may win the race with an interrupt already
                    # queued by RuntimeClient.  An interrupt of a now-idle
                    # thread is idempotent, not a session-owner failure.
                    self._resolve(command, {})
                    continue
                if command.kind != "start":
                    self._reject(command, AppServerError("Claude Code turn is not active"))
                    continue
                self._resolve(command, {})
                stop = await self._drive_turn(thread_id, command.params, queue)
                if stop:
                    return
        except asyncio.CancelledError:
            record = self._threads.get(thread_id)
            if record and record.get("activeTurnId"):
                await self._complete_turn(record, record["activeTurnId"], "interrupted", {},
                                          "Claude Code session owner was stopped")
            raise
        finally:
            error = AppServerError("Claude Code session owner is closed")
            while not queue.empty():
                self._reject(queue.get_nowait(), error)
            current = self._owners.get(thread_id)
            if current and current.task is asyncio.current_task():
                self._owners.pop(thread_id, None)

    async def _drive_turn(
        self,
        thread_id: str,
        params: dict[str, Any],
        queue: asyncio.Queue[_Command],
    ) -> bool:
        record = self._record(thread_id)
        turn_id = params["turnId"]
        client = None
        receive_task: asyncio.Task[Any] | None = None
        command_task: asyncio.Task[_Command] | None = None
        result_seen = False
        interrupt_requested = False
        stop_owner = False
        try:
            self._assert_subscription_environment()
            options = self._options(record, params)
            client = self._new_client(options)
            await client.connect()
            info = await client.get_server_info()
            self._validate_connected_account(info)
            self._validate_model_effort(record["model"], params.get("effort"), info)
            await self._validate_mcp_boundary(client)
            # A stop/interrupt can arrive while the official CLI is starting.
            # Honor it before writing the first prompt, so a slow connection
            # cannot turn an already-cancelled turn into a paid model request.
            queued: list[_Command] = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            cancelled_before_query = any(
                command.kind in {"stop", "interrupt"}
                and command.params.get("turnId", turn_id) == turn_id
                for command in queued
            )
            if cancelled_before_query:
                stop_owner = any(command.kind == "stop" for command in queued)
                for command in queued:
                    if command.kind in {"stop", "interrupt"}:
                        self._resolve(command, {})
                    else:
                        self._reject(command, AppServerError("Claude Code turn was interrupted before query"))
                await self._complete_turn(record, turn_id, "interrupted", {}, "Turn interrupted")
                return stop_owner
            await client.query(params["prompt"])
            for command in queued:
                if command.kind == "steer" and command.params.get("turnId") == turn_id:
                    try:
                        await client.query(command.params["prompt"])
                    except Exception:
                        self._reject(command, AppServerError("Claude Code control request failed"))
                        raise
                    self._resolve(command, {"turn": deepcopy(self._turn(record, turn_id))})
                else:
                    self._reject(command, AppServerError("unknown or stale Claude Code session command"))
            iterator = client.receive_response().__aiter__()
            receive_task = asyncio.create_task(anext(iterator))
            command_task = asyncio.create_task(queue.get())
            while not result_seen and not stop_owner:
                done, _ = await asyncio.wait(
                    {receive_task, command_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    try:
                        message = receive_task.result()
                    except StopAsyncIteration:
                        raise AppServerError("Claude Code response ended without a result")
                    result_seen = await self._handle_message(
                        record, turn_id, message, params.get("outputSchema"), interrupt_requested
                    )
                    if not result_seen:
                        receive_task = asyncio.create_task(anext(iterator))
                if command_task in done:
                    command = command_task.result()
                    command_task = None
                    try:
                        if command.kind == "steer":
                            if result_seen or command.params.get("turnId") != turn_id:
                                self._reject(command, AppServerError("cannot steer a completed or stale turn"))
                            else:
                                await client.query(command.params["prompt"])
                                self._resolve(command, {"turn": deepcopy(self._turn(record, turn_id))})
                        elif command.kind == "interrupt":
                            if result_seen or command.params.get("turnId") != turn_id:
                                self._resolve(command, {})
                            else:
                                interrupt_requested = True
                                await client.interrupt()
                                self._resolve(command, {})
                        elif command.kind == "stop":
                            stop_owner = True
                            interrupt_requested = True
                            try:
                                await client.interrupt()
                            except Exception:
                                pass
                            self._resolve(command, {})
                        elif command.kind == "start":
                            self._reject(command, AppServerError("Claude Code thread already has an active turn"))
                        else:
                            self._reject(command, AppServerError("unknown Claude Code session command"))
                    except asyncio.CancelledError:
                        self._reject(command, AppServerError("Claude Code control request was cancelled"))
                        raise
                    except Exception:
                        self._reject(command, AppServerError("Claude Code control request failed"))
                        raise
                    if not stop_owner:
                        command_task = asyncio.create_task(queue.get())
            if stop_owner and not result_seen:
                await self._complete_turn(record, turn_id, "interrupted", {}, "Turn interrupted")
            return stop_owner
        except asyncio.CancelledError:
            if record.get("activeTurnId") == turn_id:
                await self._complete_turn(record, turn_id, "interrupted", {}, "Turn interrupted")
            raise
        except Exception as exc:
            if record.get("activeTurnId") == turn_id:
                await self._complete_turn(record, turn_id, "failed", {}, self._safe_failure(exc))
            return stop_owner
        finally:
            # If response completion raced queue.get(), put an unprocessed
            # command back for the idle owner.  Otherwise a stop/interrupt
            # future could be orphaned after queue.get() had consumed it.
            unprocessed: _Command | None = None
            for task in (receive_task, command_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receive_task, command_task) if task), return_exceptions=True
            )
            if command_task and not command_task.cancelled() and command_task.exception() is None:
                unprocessed = command_task.result()
            if unprocessed is not None:
                queue.put_nowait(unprocessed)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _handle_message(
        self,
        record: dict[str, Any],
        turn_id: str,
        message: Any,
        schema: dict[str, Any] | None,
        interrupt_requested: bool,
    ) -> bool:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

        if isinstance(message, SystemMessage):
            session_id = message.data.get("session_id")
            if isinstance(session_id, str) and self._valid_uuid(session_id):
                record["claudeSessionId"] = session_id
                record["hasSession"] = True
                self._persist()
            return False
        if isinstance(message, AssistantMessage):
            text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
            if text:
                await self._append_agent_item(record, turn_id, text)
            return False
        if not isinstance(message, ResultMessage):
            return False
        if self._valid_uuid(message.session_id):
            record["claudeSessionId"] = message.session_id
            record["hasSession"] = True
        usage = self._json_primitives(message.usage) if isinstance(message.usage, dict) else {}
        status = "interrupted" if interrupt_requested or message.terminal_reason in {
            "aborted_streaming", "aborted_tools", "cancelled"
        } else "failed" if message.is_error else "completed"
        error: str | None = None
        if schema is not None and status == "completed":
            structured = message.structured_output
            if structured is None and isinstance(message.result, str):
                try:
                    structured = json.loads(message.result)
                except json.JSONDecodeError:
                    structured = None
            try:
                Draft202012Validator(schema).validate(structured)
            except ValidationError:
                status = "failed"
                error = "Claude Code returned output that did not satisfy outputSchema"
            else:
                canonical = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
                turn = self._turn(record, turn_id)
                if not turn["items"] or turn["items"][-1].get("text") != canonical:
                    await self._append_agent_item(record, turn_id, canonical)
        elif not self._turn(record, turn_id)["items"] and isinstance(message.result, str) and message.result:
            await self._append_agent_item(record, turn_id, message.result)
        if status == "failed" and error is None:
            error = "Claude Code reported that the turn failed"
        await self._complete_turn(record, turn_id, status, usage, error)
        return True

    async def _append_agent_item(self, record: dict[str, Any], turn_id: str, text: str) -> None:
        item = {"id": str(uuid4()), "type": "agentMessage", "text": text}
        self._turn(record, turn_id)["items"].append(item)
        self._persist()
        await self.emit(
            {
                "method": "item/completed",
                "params": {
                    "threadId": record["id"],
                    "turnId": turn_id,
                    "itemId": item["id"],
                    "item": deepcopy(item),
                },
            }
        )

    async def _complete_turn(
        self,
        record: dict[str, Any],
        turn_id: str,
        status: str,
        usage: dict[str, Any],
        error: str | None,
    ) -> None:
        turn = self._turn(record, turn_id)
        if turn.get("status") != "inProgress":
            return
        turn["status"] = status
        turn["usage"] = usage
        if error:
            turn["error"] = {"message": error}
        if record.get("activeTurnId") == turn_id:
            record.pop("activeTurnId", None)
        self._persist()
        await self.emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": record["id"],
                    "turnId": turn_id,
                    "turn": deepcopy(turn),
                },
            }
        )

    def _options(self, record: dict[str, Any], params: dict[str, Any]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        server = self._sdk_server(record)
        allowed = [f"mcp__{_MCP_SERVER}__{entry['name']}" for entry in record["tools"]]
        system_prompt = _SYSTEM_PROMPT
        if record.get("developerInstructions"):
            system_prompt += "\n\nBello developer instructions:\n" + record["developerInstructions"]
        kwargs: dict[str, Any] = {
            "tools": [],
            "allowed_tools": allowed,
            "disallowed_tools": list(_DISALLOWED_BUILTINS),
            "system_prompt": system_prompt,
            "mcp_servers": {_MCP_SERVER: server},
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",
            "cwd": record["cwd"],
            "cli_path": str(self._cli_path or self._bundled_cli_path()),
            "model": record["model"],
            "fallback_model": None,
            "setting_sources": [],
            "skills": [],
            "plugins": [],
            "hooks": None,
            "agents": None,
            "settings": json.dumps(
                {"autoMemoryEnabled": False, "disableAllHooks": True, "enabledPlugins": {}},
                separators=(",", ":"),
            ),
            "env": {
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_AGENT_SDK_CLIENT_APP": "bello/0.6.0.dev0",
            },
            "extra_args": {"disable-slash-commands": None, "no-chrome": None},
            "effort": params.get("effort"),
            "output_format": (
                {"type": "json_schema", "schema": params["outputSchema"]}
                if params.get("outputSchema") is not None
                else None
            ),
        }
        if record.get("hasSession"):
            kwargs["resume"] = record["claudeSessionId"]
        else:
            kwargs["session_id"] = record["claudeSessionId"]
        return ClaudeAgentOptions(**kwargs)

    def _metadata_options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            tools=[],
            allowed_tools=[],
            disallowed_tools=list(_DISALLOWED_BUILTINS),
            system_prompt="Bello Claude Code metadata probe. Do not call tools.",
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            cwd=self.state_dir,
            cli_path=str(self._cli_path or self._bundled_cli_path()),
            setting_sources=[],
            skills=[],
            plugins=[],
            hooks=None,
            agents=None,
            settings=json.dumps(
                {"autoMemoryEnabled": False, "disableAllHooks": True, "enabledPlugins": {}},
                separators=(",", ":"),
            ),
            env={
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_AGENT_SDK_CLIENT_APP": "bello/0.6.0.dev0",
            },
            extra_args={"disable-slash-commands": None, "no-chrome": None},
        )

    def _sdk_tools(
        self,
        record: dict[str, Any],
        server_ref: dict[str, Any],
    ) -> list[Any]:
        from claude_agent_sdk import SdkMcpTool

        result = []
        for definition in record["tools"]:
            name = definition["name"]

            async def handler(arguments: dict[str, Any], *, tool_name: str = name) -> dict[str, Any]:
                active = record.get("activeTurnId")
                if not isinstance(active, str):
                    return {
                        "content": [{"type": "text", "text": "Bello rejected a stale tool call."}],
                        "is_error": True,
                    }
                call_id = self._mcp_call_id(record, active, server_ref.get("instance"))
                if call_id is None:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Bello rejected a tool call without a stable Claude Code "
                                    "MCP request identity."
                                ),
                            }
                        ],
                        "is_error": True,
                    }
                try:
                    raw = await self.tool_handler(
                        {
                            "threadId": record["id"],
                            "turnId": active,
                            "callId": call_id,
                            "name": tool_name,
                            "arguments": arguments,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return {
                        "content": [{"type": "text", "text": "Bello tool host rejected the call."}],
                        "is_error": True,
                    }
                content = raw.get("content") if isinstance(raw, dict) else None
                if not isinstance(content, list):
                    content = [{"type": "text", "text": "Bello returned an invalid tool result."}]
                    is_error = True
                else:
                    is_error = bool(raw.get("isError", False))
                return {"content": content, "is_error": is_error}

            result.append(
                SdkMcpTool(
                    name=name,
                    description=definition["description"],
                    input_schema=definition["parameters"],
                    handler=handler,
                )
            )
        return result

    def _sdk_server(self, record: dict[str, Any]) -> Any:
        from claude_agent_sdk import create_sdk_mcp_server

        # create_sdk_mcp_server intentionally presents SdkMcpTool handlers with
        # arguments only. Keep a reference to the underlying server so the
        # handler can recover the protocol request context. MCP 2.x exposes it
        # to middleware; MCP 1.x exposes Server.request_context directly.
        server_ref: dict[str, Any] = {}
        config = create_sdk_mcp_server(
            _MCP_SERVER,
            version="0.6.0.dev0",
            tools=self._sdk_tools(record, server_ref),
        )
        server = config["instance"]
        server_ref["instance"] = server
        middleware = getattr(server, "middleware", None)
        if isinstance(middleware, list):
            middleware.insert(0, _CaptureMcpRequestContext())
        return config

    @staticmethod
    def _mcp_call_id(
        record: dict[str, Any],
        turn_id: str,
        server: Any,
    ) -> str | None:
        """Derive a replay-stable, namespaced id from the inbound MCP request.

        Claude Code currently carries its logical tool-use id in the
        ``claudecode/toolUseId`` request metadata. The JSON-RPC request id is a
        safe fallback for older clients. There is deliberately no random or
        argument-derived fallback: without protocol correlation, a mutating
        tool must fail closed instead of losing ToolHost's at-most-once fence.
        """

        context = _MCP_REQUEST_CONTEXT.get()
        if context is None and server is not None:
            try:
                context = server.request_context
            except (AttributeError, LookupError):
                context = None
        if context is None:
            return None

        source = "request"
        identity: Any = getattr(context, "request_id", None)
        meta = getattr(context, "meta", None)
        if isinstance(meta, Mapping):
            tool_use_id = meta.get("claudecode/toolUseId")
            if isinstance(tool_use_id, str) and tool_use_id:
                source = "tool-use"
                identity = tool_use_id
        if not isinstance(identity, str | int) or isinstance(identity, bool):
            return None
        if isinstance(identity, str) and not identity:
            return None

        payload = json.dumps(
            [
                "claude-code",
                record["id"],
                record["claudeSessionId"],
                turn_id,
                source,
                identity,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return "claude-code:" + hashlib.sha256(payload).hexdigest()

    async def _read_model_catalog(self) -> list[dict[str, Any]]:
        self._assert_subscription_environment()
        client = self._new_client(self._metadata_options())
        try:
            await client.connect()
            info = await client.get_server_info()
            self._validate_connected_account(info)
            await self._validate_mcp_boundary(client, allow_absent_bello=True)
            return self._catalog_entries(info)
        finally:
            await client.disconnect()

    async def _model_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Validate an exact subscription model profile without sending a prompt."""

        await self._ensure_initialized()
        if params.get("provider") != "claude-code":
            raise AppServerError(
                "Claude Code model validation requires provider 'claude-code'; "
                "Bello will not change the authentication or billing route"
            )
        model = self._model(params.get("model"))
        if self._catalog is None:
            self._catalog = await self._read_model_catalog()
        descriptor = next(
            (entry for entry in self._catalog if entry.get("id") == model),
            None,
        )
        if descriptor is None:
            raise AppServerError(
                f"Claude Code did not advertise model {model!r} for the signed-in subscription"
            )
        supported = descriptor.get("supportedReasoningEfforts")
        supported_efforts = (
            [value for value in supported if isinstance(value, str)]
            if isinstance(supported, list)
            else []
        )
        effort = params.get("effort")
        if effort is not None:
            self._validate_effort(effort, SUPPORTED_EFFORTS)
            if effort not in supported_efforts:
                choices = ", ".join(supported_efforts) or "none"
                raise AppServerError(
                    f"Claude Code model {model!r} does not advertise effort {effort!r}; "
                    f"available: {choices}. Bello will not substitute an effort"
                )
        if params.get("serviceTier") is not None:
            raise AppServerError(
                "Claude Code subscription models do not expose a selectable service tier"
            )
        return {
            "valid": True,
            "model": deepcopy(descriptor),
            "requested": {
                "effort": effort,
                "serviceTier": params.get("serviceTier"),
            },
            "execution": {
                "engine": "claude-code",
                "effort": effort,
            },
        }

    def _new_client(self, options: Any) -> Any:
        if self._client_factory is not None:
            return self._client_factory(options)
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(options)

    async def _validate_mcp_boundary(self, client: Any, *, allow_absent_bello: bool = False) -> None:
        status = await client.get_mcp_status()
        servers = status.get("mcpServers", []) if isinstance(status, dict) else []
        foreign = sorted(
            server.get("name")
            for server in servers
            if isinstance(server, dict)
            and isinstance(server.get("name"), str)
            and server["name"] != _MCP_SERVER
            and server.get("status") not in {"disabled", "failed"}
        )
        if foreign:
            raise AppServerError(
                "Claude Code managed policy exposed MCP servers outside Bello; refusing to weaken "
                "the single ToolHost boundary (servers: " + ", ".join(foreign) + ")"
            )

    def _validate_connected_account(self, info: Any) -> None:
        if not isinstance(info, dict):
            return
        account = info.get("account")
        if not isinstance(account, dict):
            return
        provider = account.get("apiProvider")
        if provider not in (None, "firstParty") or account.get("apiKeySource"):
            raise AppServerError(
                "Claude Code connected to a non-subscription provider; no model request was sent"
            )

    def _validate_model_effort(self, model: str, effort: Any, info: Any) -> None:
        if effort is None:
            return
        self._validate_effort(effort, SUPPORTED_EFFORTS)
        models = info.get("models", []) if isinstance(info, dict) else []
        match = next(
            (
                item
                for item in models
                if isinstance(item, dict)
                and model in {item.get("value"), item.get("resolvedModel")}
            ),
            None,
        )
        if match is None:
            raise AppServerError(
                f"Claude Code did not advertise effort capabilities for model {model!r}; "
                "Bello will not substitute an effort"
            )
        supported = match.get("supportedEffortLevels")
        if not isinstance(supported, list) or effort not in supported:
            choices = ", ".join(str(value) for value in supported or []) or "none"
            raise AppServerError(
                f"Claude Code model {model!r} does not advertise effort {effort!r}; "
                f"available: {choices}. Bello will not substitute an effort"
            )

    @staticmethod
    def _catalog_entries(info: Any) -> list[dict[str, Any]]:
        models = info.get("models", []) if isinstance(info, dict) else []
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            for identifier, resolved in (
                (item.get("value"), item.get("resolvedModel")),
                (item.get("resolvedModel"), item.get("resolvedModel")),
            ):
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                supported = item.get("supportedEffortLevels")
                entries.append(
                    {
                        "id": identifier,
                        "model": identifier,
                        "qualifiedId": f"claude-code/{identifier}",
                        "provider": "claude-code",
                        "name": item.get("displayName") or identifier,
                        "displayName": item.get("displayName") or identifier,
                        "resolvedModel": resolved if isinstance(resolved, str) else identifier,
                        "available": True,
                        "configured": True,
                        "reasoning": True,
                        "supportedEfforts": [
                            value for value in (supported or []) if value in SUPPORTED_EFFORTS
                        ],
                        "supportedReasoningEfforts": [
                            value for value in (supported or []) if value in SUPPORTED_EFFORTS
                        ],
                        "supportsServiceTier": False,
                        "billingRoute": "subscription",
                    }
                )
        return entries

    async def _run_auth_probe(self) -> dict[str, Any]:
        probe = self._auth_probe or self._official_auth_status
        result = probe()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise AppServerError("official Claude Code auth status returned an invalid response")
        # Discard identity and credential-source fields immediately.
        return {
            key: result.get(key)
            for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
        }

    async def _official_auth_status(self) -> dict[str, Any]:
        cli = self._cli_path or self._bundled_cli_path()
        process = await asyncio.create_subprocess_exec(
            str(cli),
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), 15)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise AppServerError("official Claude Code auth status timed out")
        if len(stdout) > 1024 * 1024:
            raise AppServerError("official Claude Code auth status response was unexpectedly large")
        try:
            data = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError("official Claude Code auth status returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise AppServerError("official Claude Code auth status returned invalid JSON")
        return data

    @staticmethod
    def _bundled_cli_path() -> Path:
        try:
            import claude_agent_sdk
        except ImportError as exc:
            raise AppServerError(
                "Claude Code support requires the pinned claude-agent-sdk package"
            ) from exc
        name = "claude.exe" if os.name == "nt" else "claude"
        path = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / name
        if not path.is_file():
            raise AppServerError("the official CLI bundled with claude-agent-sdk is missing")
        return path

    def _assert_subscription_environment(self) -> None:
        blocked = {
            name
            for name in _DIRECT_CREDENTIAL_ENV
            if str(self._environment.get(name, "")).strip()
        }
        blocked.update(
            name
            for name in _PROVIDER_SWITCH_ENV
            if str(self._environment.get(name, "")).strip().lower() not in _FALSE_ENV_VALUES
        )
        if blocked:
            raise AppServerError(
                "claude-code subscription mode refuses environment-based API/provider auth; "
                "remove these variables before starting Bello: " + ", ".join(sorted(blocked))
            )

    def _prepare_state_directory(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise AppServerError("Claude Code state directory must be a private directory, not a symlink")
        if os.name != "nt":
            os.chmod(self.state_dir, 0o700)
        if self._state_file.is_symlink():
            raise AppServerError("Claude Code state file cannot be a symbolic link")

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError("Claude Code thread state is corrupt") from exc
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION or not isinstance(data.get("threads"), dict):
            raise AppServerError("Claude Code thread state has an unsupported format")
        for thread_id, record in data["threads"].items():
            if not isinstance(thread_id, str) or not isinstance(record, dict):
                raise AppServerError("Claude Code thread state is corrupt")
            required = ("id", "model", "cwd", "tools", "claudeSessionId", "turns", "closed")
            if record.get("id") != thread_id or any(key not in record for key in required):
                raise AppServerError("Claude Code thread state is corrupt")
            if not self._valid_uuid(record["claudeSessionId"]) or not isinstance(record["turns"], list):
                raise AppServerError("Claude Code thread state is corrupt")
            active = record.pop("activeTurnId", None)
            if isinstance(active, str):
                for turn in record["turns"]:
                    if isinstance(turn, dict) and turn.get("id") == active and turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
                        turn["usage"] = {}
                        turn["error"] = {"message": "Bello restarted before Claude Code reported completion"}
            self._threads[thread_id] = record

    def _persist(self) -> None:
        payload = {"version": _STATE_VERSION, "threads": self._threads}
        descriptor, temporary = tempfile.mkstemp(prefix=".threads-", suffix=".json", dir=self.state_dir)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_file)
            if os.name != "nt":
                os.chmod(self._state_file, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass

    def _thread_list(self, params: dict[str, Any]) -> dict[str, Any]:
        records = [record for record in self._threads.values() if not record.get("closed")]
        records.sort(key=lambda record: record["id"])
        return self._page(
            [self._public_thread(record, include_turns=False) for record in records], params
        )

    def _turns_list(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._record(params.get("threadId"))
        turns = deepcopy(record["turns"])
        if params.get("sortDirection", "desc") != "asc":
            turns.reverse()
        if params.get("itemsView") == "none":
            for turn in turns:
                turn.pop("items", None)
        return self._page(turns, params)

    @staticmethod
    def _page(items: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and cursor.isdigit():
            offset = int(cursor)
        else:
            raise AppServerError("invalid pagination cursor")
        limit = params.get("limit", len(items) or 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise AppServerError("pagination limit must be an integer from 1 to 1000")
        page = items[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return {"data": page, "nextCursor": next_cursor}

    @staticmethod
    def _public_thread(record: dict[str, Any], *, include_turns: bool) -> dict[str, Any]:
        status = (
            "archived"
            if record.get("closed")
            else "active"
            if record.get("activeTurnId")
            else "idle"
        )
        result: dict[str, Any] = {
            "id": record["id"],
            "model": f"claude-code/{record['model']}",
            "qualifiedModel": f"claude-code/{record['model']}",
            "provider": "claude-code",
            "cwd": record["cwd"],
            "status": {"type": status},
            "reasoningEffort": record.get("effort"),
        }
        if record.get("parentThreadId") is not None:
            result["parentThreadId"] = record["parentThreadId"]
        if include_turns:
            result["turns"] = deepcopy(record["turns"])
        return result

    def _record(self, thread_id: Any) -> dict[str, Any]:
        if not isinstance(thread_id, str) or thread_id not in self._threads:
            raise AppServerError("unknown Claude Code thread")
        return self._threads[thread_id]

    @staticmethod
    def _turn(record: dict[str, Any], turn_id: str) -> dict[str, Any]:
        for turn in record["turns"]:
            if turn.get("id") == turn_id:
                return turn
        raise AppServerError("unknown Claude Code turn")

    @staticmethod
    def _identifier(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise AppServerError(f"Claude Code request requires a valid {name}")
        return value

    @staticmethod
    def _model(value: Any) -> str:
        if not isinstance(value, str) or not _MODEL_RE.fullmatch(value) or value.startswith("-"):
            raise AppServerError("invalid Claude Code model id")
        return value

    @staticmethod
    def _validate_tools(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise AppServerError("Claude Code tools must be a list")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict):
                raise AppServerError("invalid Claude Code tool definition")
            name, description, parameters = entry.get("name"), entry.get("description"), entry.get("parameters")
            if not isinstance(name, str) or not _TOOL_RE.fullmatch(name) or name in seen:
                raise AppServerError("invalid or duplicate Claude Code tool name")
            if not isinstance(description, str) or not isinstance(parameters, dict):
                raise AppServerError("invalid Claude Code tool definition")
            try:
                Draft202012Validator.check_schema(parameters)
            except SchemaError as exc:
                raise AppServerError(f"invalid JSON Schema for Claude Code tool {name}") from exc
            seen.add(name)
            result.append({"name": name, "description": description, "parameters": deepcopy(parameters)})
        return result

    @staticmethod
    def _input_text(value: Any) -> str:
        if not isinstance(value, list) or not value:
            raise AppServerError("Claude Code turns require non-empty text input")
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise AppServerError(
                    "Claude Code backend currently supports text input only; no unsupported input is dropped"
                )
            parts.append(item["text"])
        text = "\n\n".join(parts)
        if not text.strip():
            raise AppServerError("Claude Code turns require non-empty text input")
        return text

    @staticmethod
    def _validate_effort(effort: Any, supported: list[str] | tuple[str, ...]) -> None:
        try:
            validate_effort(effort, supported)
        except ValueError as exc:
            raise AppServerError(str(exc)) from exc

    @staticmethod
    def _resolve(command: _Command, result: dict[str, Any]) -> None:
        if not command.future.done():
            command.future.set_result(result)

    @staticmethod
    def _reject(command: _Command, error: BaseException) -> None:
        if not command.future.done():
            command.future.set_exception(error)

    @staticmethod
    def _safe_failure(error: BaseException) -> str:
        if isinstance(error, AppServerError):
            return str(error)
        name = error.__class__.__name__
        if name in {"CLIConnectionError", "ProcessError", "CLINotFoundError", "ResultError"}:
            return f"Claude Code turn failed ({name}); credentials and subprocess stderr were not logged"
        return "Claude Code turn failed before a result was received"

    @staticmethod
    def _valid_uuid(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            UUID(value)
        except ValueError:
            return False
        return True

    @classmethod
    def _json_primitives(cls, value: Any) -> Any:
        if value is None or isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list):
            return [cls._json_primitives(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): cls._json_primitives(item)
                for key, item in value.items()
                if isinstance(key, str)
            }
        return None
