"""Claude Code subscription backend built on Anthropic's official Agent SDK.

The CLI process is an execution engine, not a second policy boundary.  It has
no built-in tools and receives only Bello's in-process MCP server.  Every tool
call therefore returns to :class:`ToolHost`, which owns approvals, workspace
scope, cancellation and at-most-once dispatch.

Authentication is deliberately narrower than the Agent SDK supports.  This
backend accepts only an existing first-party ``claude.ai`` subscription login
from the unmodified official CLI: the SDK bundle or the official build Bello
explicitly pairs with that SDK release, downloads and verifies (see
``claude_cli``). It never
reads, copies, refreshes or persists credentials, and it refuses API-key and
hosted cloud-provider routes instead of silently changing the caller's
environment.
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
from supervisor.process_fence import create_subprocess_exec
from supervisor.runtime.models import validate_effort
from supervisor.runtime.claude_async import BATCH_GUIDANCE, BATCH_TOOL_NAME, ClaudeAsyncBatches
from supervisor.runtime.claude_cli import OfficialCli, resolve_official_cli, sdk_bundled_cli
from supervisor.runtime import claude_context


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


@dataclass
class _AsyncDelivery:
    content: list[dict[str, Any]]


class _ContextCancelled(Exception):
    def __init__(self, command: _Command):
        self.command = command


class _SummaryStopped(Exception):
    """A summary attempt ended at Bello's cooperative stop request."""


class _ContextPersistenceUncertain(AppServerError):
    pass


class _CheckpointFailure(AppServerError):
    """A checkpoint failed before commit, with a closed category and receipts."""

    def __init__(self, category: str, attempts: list[dict[str, Any]] | None = None):
        super().__init__(f"Claude context checkpoint failed ({category})")
        self.category = category
        self.attempts = attempts or []


# SDK exception classes whose names are safe, content-free labels.
_SDK_TRANSPORT_ERRORS = frozenset({
    "CLIConnectionError", "CLINotFoundError", "ProcessError", "ResultError",
    "CLIJSONDecodeError", "MessageParseError",
})


async def _block_native_compaction(_input: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
    # This hook neither reads the native transcript nor injects its contents.
    return {"decision": "block", "reason": "Bello manages context checkpoints between completed tool rounds."}


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
        self._async_batches: dict[tuple[str, str], ClaudeAsyncBatches] = {}
        self._async_usage: dict[tuple[str, str], dict[str, Any]] = {}
        # Turns with model work whose terminal usage could not be read (for
        # example a stopped summary whose final result never arrived). Their
        # usage is a lower bound and is reported as such, never estimated.
        self._usage_incomplete: set[tuple[str, Any]] = set()
        self._state_persistence_uncertain = False
        self._closing = False
        self._state_file = self.state_dir / "threads.json"
        if environment is not None and client_factory is None:
            raise AppServerError(
                "an alternate environment is test-only and requires an injected Claude SDK client"
            )
        if cli_path is not None and client_factory is None:
            raise AppServerError(
                "Claude Code production mode always uses the official CLI bundled with claude-agent-sdk "
                "or Bello's verified build explicitly paired with that SDK; it does not accept another path"
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
        self._cli_path = self._cli_path_override or self._official_cli_path()
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
        self._assert_state_persistence_known()
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
        context_policy = claude_context.context_policy(params.get("noNeedForContext"))
        record: dict[str, Any] = {
            "id": thread_id,
            "model": model,
            "cwd": str(cwd),
            "tools": tools,
            "developerInstructions": developer or "",
            "asyncTools": params.get("asyncTools") is True,
            "claudeSessionId": str(uuid4()),
            "hasSession": False,
            "effort": effort,
            "turns": [],
            "closed": False,
            "noNeedForContext": context_policy,
        }
        if context_policy["enabled"]:
            record["contextState"] = claude_context.new_context()
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
        self._assert_state_persistence_known()
        await self._ensure_initialized()
        record = self._record(params.get("threadId"))
        if "noNeedForContext" in params and claude_context.context_policy(params["noNeedForContext"]) != record.get("noNeedForContext", claude_context.context_policy(None)):
            raise AppServerError("resuming a Claude Code thread cannot change noNeedForContext")
        if "asyncTools" in params and params["asyncTools"] != record.get("asyncTools", False):
            raise AppServerError("resuming a Claude Code thread cannot change asyncTools")
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
        self._assert_state_persistence_known()
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
        if record.get("contextState") is not None:
            claude_context.begin_request(record["contextState"], prompt)
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
        self._assert_state_persistence_known()
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
        async_key = (thread_id, turn_id)
        client = None
        receive_task: asyncio.Task[Any] | None = None
        command_task: asyncio.Task[_Command] | None = None
        result_seen = False
        interrupt_requested = False
        stop_owner = False
        waiting_async = False
        # The result held back while late batch results were outstanding, and
        # the usage before it was merged (restored if it becomes final).
        deferred_result = None
        deferred_usage: dict[str, Any] | None = None
        if record.get("asyncTools"):
            self._async_batches[async_key] = ClaudeAsyncBatches()
        try:
            self._assert_subscription_environment()
            if record.get("contextState") is not None:
                command_task = asyncio.create_task(queue.get())
            client, checkpoint_prompt = await self._connect_context_client(record, params, command_task)
            # A stop/interrupt can arrive while the official CLI is starting.
            # Honor it before writing the first prompt, so a slow connection
            # cannot turn an already-cancelled turn into a paid model request.
            queued: list[_Command] = []
            if command_task is not None:
                if not command_task.done():
                    command_task.cancel()
                    await asyncio.gather(command_task, return_exceptions=True)
                if not command_task.cancelled():
                    queued.append(command_task.result())
            command_task = None
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
            await self._query(client, checkpoint_prompt or params["prompt"])
            for command in queued:
                if command.kind == "steer" and command.params.get("turnId") == turn_id:
                    try:
                        if record.get("contextState") is not None:
                            claude_context.append_input(record["contextState"], command.params["prompt"], steering=True)
                        await self._query(client, command.params["prompt"])
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
                    from claude_agent_sdk import ResultMessage

                    context_state = record.get("contextState")
                    batches = self._async_batches.get(async_key)
                    if context_state is not None and not isinstance(message, _AsyncDelivery):
                        claude_context.observe_message(context_state, message)
                        if isinstance(message, ResultMessage):
                            claude_context.finish_round(context_state, turn_id, async_pending=bool(batches and batches.pending))
                    internal_limit = (
                        context_state is not None and isinstance(message, ResultMessage)
                        and message.subtype == "error_max_turns"
                        and message.terminal_reason == "max_turns"
                    )
                    control = command_task.result() if command_task in done else None
                    if isinstance(message, _AsyncDelivery) and control is not None and control.kind in {"stop", "interrupt"}:
                        # A ready result must not start another paid query
                        # when cancellation is already queued in this tick.
                        waiting_async = True
                    elif isinstance(message, _AsyncDelivery) and not message.content and deferred_result is not None:
                        # Nothing is owed any more (a batch call ended without a
                        # committed response). Finish with the held-back result
                        # instead of waking the model with an empty delivery.
                        waiting_async = False
                        if deferred_usage is not None:
                            self._async_usage[async_key] = deferred_usage
                        if context_state is not None:
                            claude_context.finish_round(context_state, turn_id)
                        final, deferred_result, deferred_usage = deferred_result, None, None
                        result_seen = await self._handle_message(
                            record, turn_id, final, params.get("outputSchema"), interrupt_requested
                        )
                    elif isinstance(message, _AsyncDelivery):
                        waiting_async = False
                        deferred_result, deferred_usage = None, None
                        content = [
                            {"type": "text", "text": "Previously pending Bello tool results are now available. Use these results before finishing the task or review."},
                            *message.content,
                        ]
                        if context_state is not None:
                            claude_context.append_input(context_state, content)
                            await client.disconnect()
                            client, checkpoint_prompt = await self._connect_context_client(record, params, command_task)
                        else:
                            checkpoint_prompt = None
                        self._check_context_cancel(command_task, record if context_state is not None else None)
                        await self._query(client, checkpoint_prompt or self._async_prompt(content))
                        iterator = client.receive_response().__aiter__()
                        receive_task = asyncio.create_task(anext(iterator))
                    elif isinstance(message, ResultMessage) and batches and batches.pending and (not message.is_error or internal_limit) and not interrupt_requested and message.terminal_reason not in {"aborted_streaming", "aborted_tools", "cancelled"}:
                        # A Claude response may finish while a batch's original
                        # commands still run. Do not finalize the Bello turn or
                        # validate an early review verdict; await actual output.
                        deferred_result, deferred_usage = message, dict(self._async_usage.get(async_key, {}))
                        self._async_usage[async_key] = self._merge_usage(self._async_usage.get(async_key, {}), message.usage)
                        if self._valid_uuid(message.session_id):
                            record["claudeSessionId"] = message.session_id
                            record["hasSession"] = True
                            self._persist()
                        await iterator.aclose()
                        waiting_async = True
                        receive_task = asyncio.create_task(self._wait_async_delivery(batches))
                    elif internal_limit and not interrupt_requested:
                        # max_turns stops after the SDK finishes the current tool
                        # round. Reconnect/resume even below the checkpoint budget:
                        # an error_max_turns CLI may exit after emitting its result.
                        self._async_usage[async_key] = self._merge_usage(self._async_usage.get(async_key, {}), message.usage)
                        if self._valid_uuid(message.session_id):
                            record["claudeSessionId"] = message.session_id
                            record["hasSession"] = True
                        self._persist()
                        await iterator.aclose()
                        await client.disconnect()
                        client, checkpoint_prompt = await self._connect_context_client(record, params, command_task)
                        self._check_context_cancel(command_task, record)
                        await self._query(client, checkpoint_prompt or claude_context.CONTINUE_PROMPT)
                        iterator = client.receive_response().__aiter__()
                        receive_task = asyncio.create_task(anext(iterator))
                    else:
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
                                ready_during_steer = []
                                if waiting_async:
                                    ready_during_steer = await self._settle_async_wait_for_steer(receive_task)
                                if record.get("contextState") is not None:
                                    claude_context.append_input(record["contextState"], command.params["prompt"], steering=True)
                                    if ready_during_steer:
                                        claude_context.append_input(record["contextState"], ready_during_steer)
                                    if waiting_async:
                                        await client.disconnect()
                                        client, checkpoint_prompt = await self._connect_context_client(record, params)
                                    else:
                                        checkpoint_prompt = None
                                else:
                                    checkpoint_prompt = None
                                if record.get("contextState") is not None:
                                    self._check_context_cancel(None, record)
                                if checkpoint_prompt:
                                    await self._query(client, checkpoint_prompt)
                                elif ready_during_steer:
                                    await self._query(client, self._async_prompt([*ready_during_steer, {"type": "text", "text": command.params["prompt"]}]))
                                else:
                                    await self._query(client, command.params["prompt"])
                                if waiting_async:
                                    waiting_async = False
                                    iterator = client.receive_response().__aiter__()
                                    receive_task = asyncio.create_task(anext(iterator))
                                self._resolve(command, {"turn": deepcopy(self._turn(record, turn_id))})
                        elif command.kind == "interrupt":
                            if result_seen or command.params.get("turnId") != turn_id:
                                self._resolve(command, {})
                            else:
                                interrupt_requested = True
                                if waiting_async:
                                    receive_task.cancel()
                                    await self._async_batches[async_key].cancel()
                                    await self._complete_turn(record, turn_id, "interrupted", self._async_usage.get(async_key, {}), "Turn interrupted")
                                    result_seen = True
                                else:
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
                await self._complete_turn(record, turn_id, "interrupted", self._async_usage.get(async_key, {}), "Turn interrupted")
            return stop_owner
        except _ContextCancelled as exc:
            self._resolve(exc.command, {})
            command_task = None
            stop_owner = exc.command.kind == "stop"
            await self._complete_turn(record, turn_id, "interrupted", self._async_usage.get(async_key, {}), "Turn interrupted")
            return stop_owner
        except asyncio.CancelledError:
            if record.get("activeTurnId") == turn_id:
                await self._complete_turn(record, turn_id, "interrupted", self._async_usage.get(async_key, {}), "Turn interrupted")
            raise
        except Exception as exc:
            if record.get("activeTurnId") == turn_id:
                await self._complete_turn(record, turn_id, "failed", self._async_usage.get(async_key, {}), self._safe_failure(exc))
            return stop_owner
        finally:
            batches = self._async_batches.pop(async_key, None)
            if batches is not None:
                await batches.cancel()
            try:
                # Every path above ends its turn; a turn must still never
                # outlive the task that drives it. A no-op once it is terminal.
                await self._complete_turn(record, turn_id, "failed", self._async_usage.get(async_key, {}),
                                          "Claude Code turn ended without a result")
            except Exception:
                pass
            self._async_usage.pop(async_key, None)
            self._usage_incomplete.discard(async_key)
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

    def _check_context_cancel(self, command_task: asyncio.Task[Any] | None, record: dict[str, Any] | None = None) -> None:
        self._assert_state_persistence_known()
        if command_task is not None and command_task.done() and not command_task.cancelled():
            command = command_task.result()
            if command.kind in {"stop", "interrupt"}:
                raise _ContextCancelled(command)
        if record is not None:
            queue = self._owners[record["id"]].queue
            queued = []
            while not queue.empty():
                queued.append(queue.get_nowait())
            cancellation = next((command for command in queued if command.kind in {"stop", "interrupt"}), None)
            if cancellation is not None:
                for command in queued:
                    if command.kind in {"stop", "interrupt"}:
                        self._resolve(command, {})
                    else:
                        self._reject(command, AppServerError("Turn interrupted during context preparation"))
                if command_task is not None and command_task.done() and not command_task.cancelled():
                    self._reject(command_task.result(), AppServerError("Turn interrupted during context preparation"))
                raise _ContextCancelled(cancellation)
            for command in queued:
                queue.put_nowait(command)

    async def _connect_context_client(
        self,
        record: dict[str, Any],
        params: dict[str, Any],
        command_task: asyncio.Task[Any] | None = None,
    ) -> tuple[Any, Any]:
        """Connect at a completed SDK round, optionally replacing its context.

        Only private Bello state is replaced. Native transcripts remain intact
        and are never used as a writable history API. A failed summary or new
        connection leaves the original session ID and context untouched.
        """
        self._check_context_cancel(command_task, record)
        pending_state = record.get("contextState")
        if pending_state is not None and pending_state.get("bootstrapPending") and record.get("hasSession"):
            # The native session of a committed checkpoint exists but never
            # confirmed its bootstrap (the process stopped or the turn failed
            # first), so it may already hold all or part of it. Sending it again
            # would duplicate the quoted history there and inflate the context
            # toward another checkpoint: continue in a clean native session.
            pending_state["archivedSessions"].append(record["claudeSessionId"])
            record["claudeSessionId"] = str(uuid4())
            record["hasSession"] = False
            self._persist()
        client = self._new_client(self._options(record, params))
        try:
            await self._validate_context_connection(client, record, params)
            self._check_context_cancel(command_task, record)
            state = record.get("contextState")
            if state is None:
                return client, None
            usage = await client.get_context_usage()
            self._check_context_cancel(command_task, record)
            total = usage.get("totalTokens") if isinstance(usage, dict) else None
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise AppServerError("Claude SDK did not report reliable context usage")
            policy = record["noNeedForContext"]
            threshold = policy["thresholdTokens"]
            effective_limit = usage.get("maxTokens")
            if (not isinstance(effective_limit, int) or isinstance(effective_limit, bool)
                    or effective_limit <= 8192):
                raise AppServerError("Claude SDK did not report a usable model context capacity")
            maximum_threshold = effective_limit - max(8192, int(effective_limit * 0.05))
            if threshold > maximum_threshold:
                raise AppServerError(
                    f"Configured context threshold {threshold} exceeds the Claude model input capacity "
                    f"{maximum_threshold} after output and safety reserves; choose a compatible threshold or model"
                )
            if state.get("bootstrapPending"):
                prompt = claude_context.bootstrap_input(state)
                limit = usage.get("maxTokens")
                if not isinstance(limit, int) or total + claude_context.estimate_tokens(prompt) >= limit - 8192:
                    raise AppServerError("Claude cannot safely restore the protected context checkpoint")
                return client, prompt if isinstance(prompt, str) else self._async_prompt(prompt)
            # The configured threshold is authoritative. The owned PreCompact
            # hook blocks the SDK's independent automatic-compaction policy;
            # its advertised trigger must not silently lower Bello's setting.
            batches = self._async_batches.get((record["id"], record.get("activeTurnId")))
            older, retained = claude_context.partition_rounds(state, policy)
            projected = total + (claude_context.estimate_tokens(state["pending"]) if state["pending"] else 0)
            if projected < threshold or (batches is not None and batches.pending):
                return client, None
            if not older:
                if projected >= effective_limit - 4096:
                    raise AppServerError("Claude context cannot fit the protected task and newest whole tool round")
                return client, None
            # Required: the next request may no longer fit. Otherwise a
            # checkpoint waits for the persisted schedule: after a failed one,
            # until the context has grown by a larger step than before and
            # for at most a few consecutive failures; after a committed one,
            # until the fresh session has grown (see claude_context).
            required = projected >= effective_limit - 4096
            if not claude_context.checkpoint_due(state, threshold, projected, required=required):
                return client, None
            trigger = "inputLimit" if required else "threshold"
            prior_failures = claude_context.failed_checkpoints(state)
            fresh = None
            original_disconnected = False
            stage = "summary"
            attempts: list[dict[str, Any]] = []
            try:
                summary, summary_format, attempts = await self._context_summary(record, params, older, command_task)
                self._check_context_cancel(command_task, record)
                stage = "candidate"
                candidate = deepcopy(state)
                candidate["summary"] = summary
                candidate["rounds"] = retained
                if candidate.get("openAsyncRoundStart") is not None:
                    candidate["openAsyncRoundStart"] -= len(older)
                candidate["generation"] += 1
                candidate["bootstrapPending"] = True
                candidate["archivedSessions"].append(record["claudeSessionId"])
                prompt = claude_context.bootstrap_input(candidate)
                if claude_context.estimate_tokens(prompt) >= total:
                    raise _CheckpointFailure("not_reducing", attempts)
                if claude_context.estimate_tokens(prompt) >= effective_limit - 8192:
                    raise _CheckpointFailure("candidate_too_large", attempts)
                new_record = {**record, "claudeSessionId": str(uuid4()), "hasSession": False,
                              "contextState": candidate}
                # MCP callbacks must retain the live logical record, not a
                # snapshot whose active-turn guard would become stale.
                fresh_options = self._options(record, params)
                fresh_options.resume = None
                fresh_options.session_id = new_record["claudeSessionId"]
                # SDK connect()/disconnect() own an AnyIO cancellation scope.
                # Two clients in this task must be closed in stack order, so
                # close the idle old connection before opening the candidate.
                # Its durable transcript still provides the rollback target.
                stage = "connect"
                await client.disconnect()
                original_disconnected = True
                fresh = self._new_client(fresh_options)
                await self._validate_context_connection(fresh, new_record, params)
                stage = "measure"
                fresh_usage = await fresh.get_context_usage()
                baseline = fresh_usage.get("totalTokens") if isinstance(fresh_usage, dict) else None
                fresh_limit = fresh_usage.get("maxTokens") if isinstance(fresh_usage, dict) else None
                if (not isinstance(baseline, int) or isinstance(baseline, bool) or baseline < 0
                        or not isinstance(fresh_limit, int) or isinstance(fresh_limit, bool) or fresh_limit <= 0):
                    raise _CheckpointFailure("fresh_budget_unavailable", attempts)
                projected_fresh = baseline + claude_context.estimate_tokens(prompt)
                if projected_fresh >= min(projected, fresh_limit - 8192):
                    raise _CheckpointFailure("candidate_too_large", attempts)
                if not required and projected_fresh >= threshold:
                    # Committing would leave the fresh session at or above the
                    # threshold, so the very next round would summarize again:
                    # an immediate compaction loop. Keep the original instead.
                    raise _CheckpointFailure("not_reducing", attempts)
                self._check_context_cancel(command_task, record)
                claude_context.note_checkpoint_commit(candidate, threshold, projected_fresh)
                stage = "archive"
                self._archive_context(record)
                stage = "commit"
                self._persist_context_checkpoint(record, new_record)
                try:
                    await self.emit({"method": "thread/contextCompacted", "params": {
                        "threadId": record["id"], "generation": candidate["generation"],
                        "retainedRounds": len(retained), "summarizedRounds": len(older),
                        "previousTokens": total, "freshTokens": projected_fresh,
                        "configuredThresholdTokens": policy["thresholdTokens"],
                        "effectiveThresholdTokens": threshold,
                        "historyRepresentation": "quoted-transcript",
                        "trigger": trigger, "summaryFormat": summary_format, "summaryChars": len(summary),
                        "attempts": attempts, **self._summary_budget_fields(),
                        "priorFailedCheckpoints": prior_failures,
                    }})
                except Exception:
                    pass  # The checkpoint is already committed durably.
                return fresh, prompt if isinstance(prompt, str) else self._async_prompt(prompt)
            except (_ContextCancelled, asyncio.CancelledError, _ContextPersistenceUncertain):
                if fresh is not None:
                    await fresh.disconnect()
                raise
            except Exception as error:
                if fresh is not None:
                    await fresh.disconnect()
                if original_disconnected:
                    client = self._new_client(self._options(record, params))
                    await self._validate_context_connection(client, record, params)
                category = self._checkpoint_category(error, stage)
                schedule = claude_context.note_checkpoint_failure(state, threshold, projected)
                self._persist_schedule()
                try:
                    await self.emit({"method": "thread/contextCompactionFailed", "params": {
                        "threadId": record["id"], "message": "Context checkpoint failed; the original session was preserved.",
                        "generation": state["generation"], "trigger": trigger,
                        "stage": "summary" if stage == "summary" else "replacement",
                        "category": category,
                        "attempts": error.attempts if isinstance(error, _CheckpointFailure) else attempts,
                        "previousTokens": total, "projectedTokens": projected,
                        "summarizedRounds": len(older), "retainedRounds": len(retained),
                        **self._summary_budget_fields(),
                        "configuredThresholdTokens": threshold, **schedule,
                        "originalContextContinues": not required,
                    }})
                except Exception:
                    pass  # Telemetry is best effort; the original context is intact.
                if required:
                    raise AppServerError(
                        f"Claude context checkpoint failed ({category}) and the original context cannot safely continue"
                    )
                return client, None
        except BaseException:
            await client.disconnect()
            raise

    async def _validate_context_connection(self, client: Any, record: dict[str, Any], params: dict[str, Any]) -> None:
        await client.connect()
        info = await client.get_server_info()
        self._validate_connected_account(info)
        self._validate_model_effort(record["model"], params.get("effort"), info)
        await self._validate_mcp_boundary(client)

    def _summary_options(self, record: dict[str, Any], params: dict[str, Any]) -> Any:
        """The role's model, effort and account boundary, with no tools.

        ``output_format`` stays unset on purpose. The pinned CLI implements a
        JSON schema as a synthetic StructuredOutput tool that it adds even with
        ``tools=[]``; a plain-text answer then gets an injected corrective turn,
        which ``max_turns=1`` ends as ``error_max_turns``. The summary is an
        ordinary plain-text handoff validated by ``claude_context.inspect_summary``.
        ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` is forwarded by the managed CLI as the
        request's ``max_tokens``, which adaptive thinking shares with the text.
        """
        options = self._options(record, params)
        options.system_prompt = claude_context.SUMMARY_SYSTEM
        options.mcp_servers = {}
        options.allowed_tools = []
        options.tools = []
        options.resume = None
        options.session_id = str(uuid4())
        options.output_format = None
        options.max_turns = 1
        options.env = {**options.env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(claude_context.SUMMARY_MAX_OUTPUT_TOKENS)}
        return options

    @staticmethod
    def _summary_budget_fields() -> dict[str, int]:
        """The effective summary budget, as content-free receipt fields."""
        return {"summaryTimeoutSeconds": claude_context.SUMMARY_TIMEOUT_SECONDS,
                "summaryMaxOutputTokens": claude_context.SUMMARY_MAX_OUTPUT_TOKENS}

    @staticmethod
    def _summary_failure(error: BaseException) -> tuple[str, dict[str, Any]]:
        """Map one failed attempt to a closed category and content-free details."""
        if isinstance(error, claude_context.SummaryRejected):
            return error.category, dict(error.details)
        if isinstance(error, TimeoutError):
            return "timeout", {}
        name = type(error).__name__
        if name in _SDK_TRANSPORT_ERRORS:
            return "transport_error", {"errorType": name}
        return "unexpected_error", {}

    @staticmethod
    def _checkpoint_category(error: BaseException, stage: str) -> str:
        if isinstance(error, _CheckpointFailure):
            return error.category
        return {
            "candidate": "candidate_invalid", "connect": "candidate_connection_failed",
            "measure": "fresh_budget_unavailable", "archive": "archive_failed", "commit": "commit_failed",
        }.get(stage, "unexpected_error")

    async def _context_summary(
        self, record: dict[str, Any], params: dict[str, Any], older: list,
        command_task: asyncio.Task[Any] | None,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Return ``(summary, format, attempt receipts)`` or raise _CheckpointFailure.

        The whole step, its retry included, shares ONE deadline of
        ``SUMMARY_TIMEOUT_SECONDS``: a second attempt only runs in the time the
        first left, and an attempt that exhausted it is not retried.

        Each attempt task owns its summarizer from connect to disconnect. An
        interrupt, a stop or cancellation of this task reaches it only as a
        cooperative stop request, never as ``Task.cancel()``: the pinned SDK's
        AnyIO-shielded transport close does not defer an asyncio cancellation,
        and a query that may have been submitted must still be interrupted and
        its final usage read. Cancellation is never a checkpoint failure.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + claude_context.SUMMARY_TIMEOUT_SECONDS

        async def attempt(note: str | None, stream: claude_context.SummaryStream,
                          receipt: dict[str, Any], stop: asyncio.Future[None]) -> tuple[str, str]:
            from claude_agent_sdk import ResultMessage

            summarizer = self._new_client(self._summary_options(record, params))
            key = (record["id"], record.get("activeTurnId"))
            # Set before the query is written, not after its await returns: a
            # cancelled or timed-out write may already have reached the CLI.
            dispatched = False
            dispatch: asyncio.Task[None] | None = None
            reading: asyncio.Task[Any] | None = None
            submitted_at: float | None = None

            def honor_stop() -> None:
                # Setup steps are bounded and finish first: cancelling SDK
                # connect could abort its own shielded cleanup. No query is
                # pending yet, so nothing needs an interrupt.
                if stop.done():
                    raise _SummaryStopped()

            try:
                # The shared deadline; see SUMMARY_TIMEOUT_SECONDS for exactly
                # what it covers. The stop/drain and the transport close below
                # run after it, under their own short bounds.
                async with asyncio.timeout_at(deadline):
                    receipt["phase"] = "connect"
                    honor_stop()
                    try:
                        await self._validate_context_connection(summarizer, record, params)
                    except AppServerError as error:
                        raise claude_context.SummaryRejected("connection_rejected") from error
                    honor_stop()
                    status = await summarizer.get_mcp_status()
                    if any(server.get("status") not in {"disabled", "failed"}
                           for server in status.get("mcpServers", [])):
                        raise claude_context.SummaryRejected("tools_exposed")
                    honor_stop()
                    self._check_context_cancel(command_task, record)
                    receipt["phase"] = "measure"
                    prompt = claude_context.summary_prompt(record["contextState"], older, note=note)
                    summary_usage = await summarizer.get_context_usage()
                    summary_limit = summary_usage.get("maxTokens") if isinstance(summary_usage, dict) else None
                    summary_baseline = summary_usage.get("totalTokens") if isinstance(summary_usage, dict) else None
                    if (not isinstance(summary_limit, int) or isinstance(summary_limit, bool) or summary_limit <= 0
                            or not isinstance(summary_baseline, int) or isinstance(summary_baseline, bool)
                            or summary_baseline < 0):
                        raise claude_context.SummaryRejected("usage_unavailable")
                    receipt["inputTokensEstimate"] = claude_context.estimate_tokens(prompt)
                    # The whole combined output/reasoning cap plus a margin for
                    # the character estimate must fit before anything is sent;
                    # neither the cap nor the effort is lowered to make it fit.
                    admission = claude_context.summary_admission(
                        summary_baseline, receipt["inputTokensEstimate"], summary_limit)
                    receipt.update(contextBaselineTokens=summary_baseline, contextCapacityTokens=summary_limit,
                                   admissionRequiredTokens=admission["requiredTokens"])
                    if not admission["fits"]:
                        raise claude_context.SummaryRejected("input_too_large")
                    honor_stop()
                    self._check_context_cancel(command_task, record)
                    receipt["phase"] = "dispatch"
                    dispatched = True
                    dispatch = asyncio.create_task(
                        self._query(summarizer, prompt if isinstance(prompt, str) else self._async_prompt(prompt)))
                    if await self._stop_requested_first(dispatch, stop):
                        raise _SummaryStopped()
                    dispatch, sent = None, dispatch
                    sent.result()
                    submitted_at = loop.time()
                    receipt["phase"] = "await_first_message"
                    iterator = summarizer.receive_response().__aiter__()
                    while True:
                        honor_stop()
                        reading = asyncio.create_task(anext(iterator))
                        if await self._stop_requested_first(reading, stop):
                            raise _SummaryStopped()  # the drain still consults this read
                        reading, read = None, reading
                        try:
                            message = read.result()
                        except StopAsyncIteration:
                            raise claude_context.SummaryRejected("no_result") from None
                        if receipt["phase"] == "await_first_message":
                            receipt["phase"] = "streaming"
                            receipt["firstMessageMs"] = max(0, int((loop.time() - submitted_at) * 1000))
                        if isinstance(message, ResultMessage):
                            # This fresh session's result usage covers all of its
                            # model responses; it is the only usage counted.
                            receipt["phase"] = "result"
                            receipt["responseMs"] = max(0, int((loop.time() - submitted_at) * 1000))
                            self._count_summary_usage(key, receipt, message)
                            stream.observe_result(message)
                            return claude_context.inspect_summary(message, stream)
                        # Raises at a tool call, provider error or injected
                        # continuation, before the CLI's own result.
                        stream.observe(message)
            except BaseException:
                # Failure, timeout or stop: a query that may be in flight is
                # interrupted and drained by the task that owns the client.
                if dispatch is not None:
                    await self._discard_task(dispatch)
                if dispatched and not receipt.get("usageReported"):
                    pending, reading = reading, None
                    await self._stop_summarizer(summarizer, key, receipt, pending)
                raise
            finally:
                if reading is not None:
                    await self._discard_task(reading)
                try:
                    await summarizer.disconnect()
                except Exception:
                    # A delivered result stays valid; record the cleanup fault.
                    receipt["cleanupError"] = True

        attempts: list[dict[str, Any]] = []
        note: str | None = None
        for number in range(1, claude_context.SUMMARY_ATTEMPTS + 1):
            if number > 1 and loop.time() >= deadline:
                break  # the one summary deadline is spent: there is no time for a retry
            self._check_context_cancel(command_task, record)
            stream = claude_context.SummaryStream()
            receipt: dict[str, Any] = {"attempt": number, "retryNote": True if note else None}
            started = loop.time()
            stop: asyncio.Future[None] = loop.create_future()
            task = asyncio.create_task(attempt(note, stream, receipt, stop))
            watcher = None
            held: list[_Command] = []
            try:
                # A queued steering message must not hide a later interrupt
                # for the duration of a summary. Observe further controls while
                # preserving their original FIFO order for the owner loop.
                queue = self._owners[record["id"]].queue
                while not task.done():
                    watch_primary = command_task is not None and not command_task.done()
                    watcher = command_task if watch_primary else asyncio.create_task(queue.get())
                    await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
                    if watcher.done():
                        command = watcher.result()
                        if not watch_primary:
                            held.append(command)
                        if command.kind in {"stop", "interrupt"}:
                            for pending in held:
                                if pending is not command:
                                    self._reject(pending, AppServerError("Turn interrupted during context preparation"))
                            held.clear()
                            if command_task is not None and command_task.done() and command_task is not watcher:
                                self._reject(command_task.result(), AppServerError("Turn interrupted during context preparation"))
                            raise _ContextCancelled(command)
                    elif not watch_primary:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
                    watcher = None
                queued = []
                while not queue.empty():
                    queued.append(queue.get_nowait())
                for command in [*held, *queued]:
                    queue.put_nowait(command)
                self._check_context_cancel(command_task, record)
                summary, summary_format = task.result()
            except (_ContextCancelled, asyncio.CancelledError, _ContextPersistenceUncertain) as error:
                helpers: list[asyncio.Task[Any]] = []
                if watcher is not None and watcher is not command_task:
                    if not watcher.done():
                        watcher.cancel()
                        helpers.append(watcher)
                    elif not watcher.cancelled() and watcher.exception() is None:
                        # A control obtained in the tick this task was cancelled.
                        late = watcher.result()
                        if late is not getattr(error, "command", None) and all(item is not late for item in held):
                            held.append(late)
                for pending in held:
                    if pending.kind not in {"stop", "interrupt"}:
                        self._reject(pending, AppServerError("Context preparation was interrupted"))
                cancelled_again = await self._settle_summary_attempt(task, stop, helpers)
                for pending in held:
                    if pending.kind in {"stop", "interrupt"}:
                        self._resolve(pending, {})  # honored by this cancellation
                if cancelled_again and not isinstance(error, asyncio.CancelledError):
                    # This task was itself cancelled during the bounded cleanup.
                    # Acknowledge the command already honored, then propagate
                    # the cancellation instead of swallowing it.
                    if isinstance(error, _ContextCancelled):
                        self._resolve(error.command, {})
                    raise asyncio.CancelledError() from error
                raise
            except Exception as error:
                category, details = self._summary_failure(error)
                attempts.append(self._attempt_receipt(receipt, stream, started, outcome=category, **details))
                if not claude_context.retryable(category):
                    break
                note = claude_context.retry_note(category, details)
                continue
            attempts.append(self._attempt_receipt(receipt, stream, started, outcome="accepted",
                                                  summaryFormat=summary_format, summaryChars=len(summary)))
            return summary, summary_format, attempts
        raise _CheckpointFailure(attempts[-1]["outcome"] if attempts else "unexpected_error", attempts)

    async def _stop_summarizer(self, summarizer: Any, key: tuple[str, Any], receipt: dict[str, Any],
                               pending: asyncio.Task[Any] | None = None) -> None:
        """Interrupt an unfinished summary and count its final usage once.

        Closing stdin alone lets the CLI finish its own output-limit
        continuations (further paid requests) before the SDK's exit grace
        period ends. The interrupt stops that loop; the CLI's single result for
        this fresh session then reports usage that was not yet merged.

        ``pending`` is a read already waiting when the attempt stopped. It gets
        the stream's next message first, so it is consulted together with a
        fresh read; whichever delivers the terminal result is counted, once.
        A terminal result that a read has ALREADY delivered is counted first:
        the response is over, so nothing is left to interrupt, and a failing
        interrupt can never discard usage that was available. Interrupt and
        drain share one bound. A missing or later result is never estimated:
        the receipt and the turn are marked incomplete instead.
        """
        from claude_agent_sdk import ResultMessage

        readers: set[asyncio.Task[Any]] = {pending} if pending is not None else set()
        try:
            if self._count_delivered_result(key, receipt, readers):
                return
            async with asyncio.timeout(claude_context.SUMMARY_STOP_TIMEOUT_SECONDS):
                try:
                    await summarizer.interrupt()
                except Exception:
                    # The control request failed: count a result delivered in
                    # the meantime, but never wait for one that may not come.
                    self._count_delivered_result(key, receipt, readers)
                    return
                fresh = summarizer.receive_response().__aiter__()
                following = asyncio.create_task(anext(fresh))
                readers.add(following)
                while readers:
                    done, _ = await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
                    for reader in done:
                        readers.discard(reader)
                        if reader.cancelled() or reader.exception() is not None:
                            continue  # this read ended without a message
                        message = reader.result()
                        if isinstance(message, ResultMessage):
                            self._count_summary_usage(key, receipt, message)
                            return
                        if reader is following:
                            following = asyncio.create_task(anext(fresh))
                            readers.add(following)
        except Exception:
            pass  # timeout or closed stream: recorded below
        finally:
            if not receipt.get("usageReported"):
                # A read that finished within the bound delivered real usage.
                self._count_delivered_result(key, receipt, readers)
            for reader in readers:
                await self._discard_task(reader)
            if not receipt.get("usageReported"):
                receipt["stopIncomplete"] = True
                self._usage_incomplete.add(key)

    def _count_delivered_result(self, key: tuple[str, Any], receipt: dict[str, Any],
                                readers: set[asyncio.Task[Any]]) -> bool:
        """Count a terminal result that a finished read already delivered, once.

        Finished reads are removed from ``readers``; unfinished ones are left
        for the caller. Never waits and never cancels anything.
        """
        from claude_agent_sdk import ResultMessage

        for reader in [reader for reader in readers if reader.done()]:
            readers.discard(reader)
            if reader.cancelled() or reader.exception() is not None:
                continue
            message = reader.result()
            if isinstance(message, ResultMessage):
                self._count_summary_usage(key, receipt, message)
                return True
        return False

    def _count_summary_usage(self, key: tuple[str, Any], receipt: dict[str, Any], message: Any) -> None:
        """Merge one summary session's terminal usage at most once per attempt."""
        if receipt.get("usageReported"):
            return
        self._async_usage[key] = self._merge_usage(self._async_usage.get(key, {}), message.usage)
        receipt["usageReported"] = True

    @staticmethod
    async def _stop_requested_first(work: asyncio.Future[Any], stop: asyncio.Future[None]) -> bool:
        """Wait for ``work`` or a stop request, cancelling neither.

        Completed work wins a tie, so a result delivered in the same tick as
        the stop request is still observed, and its usage counted, once.
        """
        if not work.done() and not stop.done():
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        return not work.done()

    @staticmethod
    async def _discard_task(task: asyncio.Task[Any]) -> None:
        """Cancel a summary read or write helper and wait until it has ended."""
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved; the outcome is no longer needed

    @staticmethod
    async def _settle_summary_attempt(task: asyncio.Task[Any], stop: asyncio.Future[None],
                                      helpers: list[asyncio.Task[Any]]) -> bool:
        """Ask a summary attempt to stop and wait for its bounded cleanup.

        The attempt interrupts, drains and disconnects its own client in its
        own task; it is never cancelled from here. Further cancellation of this
        task is deferred until that cleanup has ended and is then reported, so
        the caller propagates it: no background work outlives the request.
        """
        if not stop.done():
            stop.set_result(None)
        cancelled = False
        waiting = [item for item in (task, *helpers) if not item.done()]
        while waiting:
            try:
                await asyncio.wait(waiting)
            except asyncio.CancelledError:
                cancelled = True
            waiting = [item for item in waiting if not item.done()]
        if not task.cancelled():
            task.exception()  # a stopped attempt's outcome is not a checkpoint result
        return cancelled

    @staticmethod
    def _attempt_receipt(receipt: dict[str, Any], stream: claude_context.SummaryStream,
                         started: float, **fields: Any) -> dict[str, Any]:
        """One bounded attempt record: categories, protocol enums and counts only."""
        values = {**receipt, **fields, **stream.summary(),
                  "durationMs": max(0, int((asyncio.get_running_loop().time() - started) * 1000))}
        return {key: value for key, value in values.items() if value is not None}

    def _archive_context(self, record: dict[str, Any]) -> None:
        directory = self.state_dir / "context-archives"
        if directory.is_symlink():
            raise AppServerError("Claude context archive directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".context-", suffix=".json", dir=directory)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump({"nativeSessionId": record["claudeSessionId"], "contextState": record["contextState"]},
                          handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            target = directory / f"{record['claudeSessionId']}-{record['contextState']['generation']}.json"
            os.replace(temporary, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
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
        usage = self._merge_usage(self._async_usage.get((record["id"], turn_id), {}), usage)
        status = "interrupted" if interrupt_requested or message.terminal_reason in {
            "aborted_streaming", "aborted_tools", "cancelled"
        } else "failed" if message.is_error else "completed"
        error: str | None = None
        output_schema_failure = False
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
                output_schema_failure = True
            else:
                canonical = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
                turn = self._turn(record, turn_id)
                if not turn["items"] or turn["items"][-1].get("text") != canonical:
                    await self._append_agent_item(record, turn_id, canonical)
        elif not self._turn(record, turn_id)["items"] and isinstance(message.result, str) and message.result:
            await self._append_agent_item(record, turn_id, message.result)
        if status == "failed" and error is None:
            error = "Claude Code reported that the turn failed"
        await self._complete_turn(
            record, turn_id, status, usage, error,
            output_schema_failure=output_schema_failure,
        )
        return True

    @staticmethod
    async def _wait_async_delivery(batches: ClaudeAsyncBatches) -> _AsyncDelivery:
        return _AsyncDelivery(await batches.take_ready(wait=True))

    @staticmethod
    async def _settle_async_wait_for_steer(task: asyncio.Task[Any]) -> list[dict[str, Any]]:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Completion may have raced the asyncio.wait done-set and already
        # claimed late results. Preserve them alongside the real steering
        # message instead of discarding a successfully completed waiter.
        if not task.cancelled() and task.exception() is None:
            delivery = task.result()
            if isinstance(delivery, _AsyncDelivery):
                return delivery.content
        return []

    @staticmethod
    async def _async_prompt(content: list[dict[str, Any]]):
        """Append output via the documented SDK streaming-input interface."""
        blocks = []
        for block in content:
            if block.get("type") == "image":
                blocks.append(deepcopy(block) if "source" in block else {"type": "image", "source": {"type": "base64", "media_type": block["mimeType"], "data": block["data"]}})
            else:
                blocks.append(deepcopy(block))
        yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}

    @staticmethod
    def _merge_usage(previous: dict[str, Any], current: Any) -> dict[str, Any]:
        result = deepcopy(previous)
        if not isinstance(current, dict):
            return result
        for key, value in current.items():
            if isinstance(value, dict):
                result[key] = ClaudeBackend._merge_usage(result.get(key, {}) if isinstance(result.get(key), dict) else {}, value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = (result.get(key, 0) if isinstance(result.get(key, 0), (int, float)) else 0) + value
            else:
                result[key] = deepcopy(value)
        return result

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
        *,
        output_schema_failure: bool = False,
    ) -> None:
        turn = self._turn(record, turn_id)
        if turn.get("status") != "inProgress":
            return
        turn["status"] = status
        turn["usage"] = usage
        if (record["id"], turn_id) in self._usage_incomplete:
            # Adapter-owned and content-free: some model work's terminal usage
            # was unavailable, so ``usage`` is a lower bound, not an estimate.
            self._usage_incomplete.discard((record["id"], turn_id))
            turn["usageIncomplete"] = True
        if error:
            turn["error"] = {"message": error}
            if output_schema_failure:
                # Adapter-owned classification, never copied from provider text.
                turn["error"]["belloFailureKind"] = "output_schema_validation"
        if record.get("activeTurnId") == turn_id:
            record.pop("activeTurnId", None)
        if record.get("contextState") is not None:
            record["contextState"]["openAsyncRoundStart"] = None
        if not self._state_persistence_uncertain:
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
        if record.get("asyncTools") and record["tools"]:
            allowed.append(f"mcp__{_MCP_SERVER}__{BATCH_TOOL_NAME}")
            system_prompt += "\n\n" + BATCH_GUIDANCE
        kwargs: dict[str, Any] = {
            "tools": [],
            "allowed_tools": allowed,
            "disallowed_tools": list(_DISALLOWED_BUILTINS),
            "system_prompt": system_prompt,
            "mcp_servers": {_MCP_SERVER: server},
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",
            "cwd": record["cwd"],
            "cli_path": str(self._cli_path or self._official_cli_path()),
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
                "CLAUDE_AGENT_SDK_CLIENT_APP": "bello/0.8.0",
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
        if record.get("noNeedForContext", {}).get("enabled"):
            from claude_agent_sdk import HookMatcher

            # Checkpoints need one SDK turn per round (Bello continues between
            # rounds) and the SDK's own compaction blocked. Permissions, tools,
            # readable roots and approvals are exactly those of a session
            # without checkpoints: the private archive restricts nothing.
            kwargs["max_turns"] = 1
            kwargs["hooks"] = {"PreCompact": [HookMatcher(hooks=[_block_native_compaction])]}
            kwargs["settings"] = json.dumps(
                {"autoMemoryEnabled": False, "disableAllHooks": False, "enabledPlugins": {}},
                separators=(",", ":"),
            )
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
            cli_path=str(self._cli_path or self._official_cli_path()),
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
                "CLAUDE_AGENT_SDK_CLIENT_APP": "bello/0.8.0",
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
        if record.get("asyncTools") and record["tools"]:
            if any(definition["name"] == BATCH_TOOL_NAME for definition in record["tools"]):
                raise AppServerError("run_parallel_tools is reserved by the Claude async adapter")
            definitions = {definition["name"]: definition for definition in record["tools"]}
            schema = {
                "type": "object",
                "properties": {"calls": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": list(definitions)}, "arguments": {"type": "object"}},
                    "required": ["name", "arguments"], "additionalProperties": False,
                }}},
                "required": ["calls"], "additionalProperties": False,
            }

            async def batch_handler(arguments: dict[str, Any]) -> dict[str, Any]:
                active = record.get("activeTurnId")
                coordinator = self._async_batches.get((record["id"], active))
                batch_id = self._mcp_call_id(record, active, server_ref.get("instance")) if isinstance(active, str) else None
                if coordinator is None or batch_id is None:
                    return {"content": [{"type": "text", "text": "Bello rejected a stale or uncorrelated asynchronous batch."}], "is_error": True}
                try:
                    Draft202012Validator(schema).validate(arguments)
                    for call in arguments["calls"]:
                        Draft202012Validator(definitions[call["name"]]["parameters"]).validate(call["arguments"])
                except ValidationError:
                    return {"content": [{"type": "text", "text": "Invalid batch: use supplied tool names and their exact argument schemas. No calls were executed."}], "is_error": True}

                async def dispatch(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
                    return await self.tool_handler({"threadId": record["id"], "turnId": active, "callId": call_id, "name": name, "arguments": args})

                try:
                    return await coordinator.run(batch_id, arguments["calls"], dispatch)
                except ValueError:
                    return {"content": [{"type": "text", "text": "Bello rejected a reused batch ID with changed calls."}], "is_error": True}

            result.append(SdkMcpTool(name=BATCH_TOOL_NAME, description=BATCH_GUIDANCE, input_schema=schema, handler=batch_handler))
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
            version="0.8.0",
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
        if effort is not None:
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
            # Checked even when no effort is sent (a `default` profile), before
            # any prompt: the model itself is not offered by this CLI/account.
            raise AppServerError(
                f"Claude Code did not advertise model {model!r} for this subscription and CLI version; "
                "no model request was sent. Bello will not substitute another model or effort"
            )
        if effort is None:
            return
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
        rows = [item for item in models if isinstance(item, dict)]

        # Describe an exact model by the row about that model: its own row, else
        # a family alias; never by the "default" recommendation row alone.
        def specificity(item: dict[str, Any]) -> int:
            if item.get("value") == item.get("resolvedModel"):
                return 0
            return 2 if item.get("value") == "default" else 1

        describing: dict[str, dict[str, Any]] = {}
        for item in rows:
            resolved = item.get("resolvedModel")
            if isinstance(resolved, str) and resolved and (
                resolved not in describing or specificity(item) < specificity(describing[resolved])
            ):
                describing[resolved] = item
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows:
            for identifier, resolved in (
                (item.get("value"), item.get("resolvedModel")),
                (item.get("resolvedModel"), item.get("resolvedModel")),
            ):
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                resolved_id = resolved if isinstance(resolved, str) and resolved else identifier
                alias = identifier != resolved_id
                source = item if alias else describing.get(identifier, item)
                # Efforts come from the first row that names the model, exactly
                # as turn-time validation matches it.
                supported = item.get("supportedEffortLevels")
                display = source.get("displayName") or identifier
                entry = {
                    "id": identifier,
                    "model": identifier,
                    "qualifiedId": f"claude-code/{identifier}",
                    "provider": "claude-code",
                    "name": display,
                    "displayName": display,
                    "resolvedModel": resolved_id,
                    # A floating alias ("sonnet", "default") may resolve to a
                    # different model after a CLI update; an exact id does not.
                    "alias": alias,
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
                description = ClaudeBackend._catalog_text(source.get("description"))
                if description is not None:
                    entry["description"] = description
                entries.append(entry)
        return entries

    @staticmethod
    def _catalog_text(value: Any) -> str | None:
        """CLI catalog descriptions are display metadata; keep them short and inert."""
        if not isinstance(value, str):
            return None
        text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
        return text[:200] or None

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
        cli = self._cli_path or self._official_cli_path()
        process = await create_subprocess_exec(
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
        """The CLI inside the installed SDK wheel; raises if the wheel has none."""
        return sdk_bundled_cli()

    @staticmethod
    def _official_cli(*, prepare: bool = False) -> OfficialCli:
        """The only CLI production mode may execute (shared readiness contract).

        ``prepare=True`` may download Bello's explicitly paired official build;
        otherwise local files are only verified, without falling back to an
        older SDK bundle when the managed build is not ready.
        """
        return resolve_official_cli(prepare=prepare, bundled=lambda: ClaudeBackend._bundled_cli_path())

    @staticmethod
    def _official_cli_path() -> Path:
        return ClaudeBackend._official_cli().path

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
            state = record.get("contextState")
            if isinstance(state, dict):
                # Asynchronous batches live in the previous process: no late
                # result can ever close an async round that was open when it
                # stopped, so it must not swallow every later round.
                state["openAsyncRoundStart"] = None
            self._threads[thread_id] = record

    def _persist(self) -> None:
        self._assert_state_persistence_known()
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

    def _persist_schedule(self) -> None:
        """Persist the checkpoint schedule after a failed checkpoint, best effort.

        The schedule only delays the next attempt: if this write fails, the
        running backend still follows it from memory and a resumed thread
        re-evaluates sooner. The ambiguous-commit latch still blocks inference.
        """
        try:
            self._persist()
        except Exception:
            pass

    def _assert_state_persistence_known(self) -> None:
        if self._state_persistence_uncertain:
            raise _ContextPersistenceUncertain(
                "Claude checkpoint commit status is unknown; restart the backend before further inference or state writes"
            )

    async def _query(self, client: Any, prompt: Any) -> None:
        # Every main-model and summarizer query passes this shared latch. The
        # state file contains all logical threads, so an uncertain write blocks
        # the whole backend until a fresh instance validates its durable state.
        self._assert_state_persistence_known()
        await client.query(prompt)

    def _persist_context_checkpoint(self, record: dict[str, Any], candidate: dict[str, Any]) -> None:
        self._assert_state_persistence_known()
        previous = {key: record[key] for key in ("claudeSessionId", "hasSession", "contextState")}
        try:
            previous_bytes = self._state_file.read_bytes()
        except OSError:
            previous_bytes = None
        record.update({key: candidate[key] for key in previous})
        try:
            expected_bytes = json.dumps(
                {"version": _STATE_VERSION, "threads": self._threads},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        except BaseException:
            record.update(previous)
            raise
        try:
            # This writer is synchronous: cancellation cannot interleave an
            # await between publication and the following reconciliation.
            self._persist()
        except BaseException as error:
            try:
                actual_bytes = None if self._state_file.is_symlink() else self._state_file.read_bytes()
            except OSError:
                actual_bytes = None
            if actual_bytes == expected_bytes:
                # rename may have succeeded before chmod/cleanup (or its caller)
                # raised. The temporary file was already fchmod(0600), and the
                # exact candidate now owns the durable session identity.
                if not isinstance(error, Exception):
                    raise
                return
            if previous_bytes is not None and actual_bytes == previous_bytes:
                record.update(previous)
                raise
            # Do not overwrite unknown durable state during failure completion,
            # nor reconnect the previous session and silently continue inference.
            self._state_persistence_uncertain = True
            if not isinstance(error, Exception):
                raise
            raise _ContextPersistenceUncertain(
                "Claude checkpoint commit status is unknown; restart the backend before further inference or state writes"
            ) from error

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
