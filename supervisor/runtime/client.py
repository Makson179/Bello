"""Provider-neutral controller boundary with Bello-owned session/tool scope.

The thread/turn/item names are an internal compatibility contract for Bello's
existing safety logic. Subscription Codex uses its native app-server; Claude
Code uses its SDK and other providers retain Pi.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from supervisor.appserver import AppServerClient, AppServerError, AppServerMessage, AppServerTimeoutError
from supervisor.approvals import ApprovalManager, normalize_approval_request
from supervisor.runtime.cleanup import finish_cleanup
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.models import parse_model_selection
from supervisor.runtime.sandbox import SandboxPolicy
from supervisor.runtime.tools import ToolHost, ToolScope, tool_result, tool_definitions
from supervisor.runtime.transport import WorkerTransport


class RuntimeClient(AppServerClient):
    """Route the established controller contract to an explicit provider engine."""

    def __init__(self, *, cwd: Path, notification_handler=None, server_request_handler=None,
                 transport_error_handler=None, state_dir: Path | None = None, backends: dict | None = None,
                 required_models: tuple[str, ...] = ()):
        super().__init__(command=[], cwd=cwd, notification_handler=notification_handler,
                         server_request_handler=server_request_handler, transport_error_handler=transport_error_handler)
        self.command = []
        self.cwd = cwd.resolve()
        self.state_dir = state_dir or self.cwd / ".supervisor" / "engines"
        self._journal: RuntimeJournal | None = None
        self._threads: dict[str, dict[str, Any]] = {}
        self._engines: dict[str, Any] = backends or {}
        self._approval_waiters: dict[str, asyncio.Future] = {}
        self._recent: deque[AppServerMessage] = deque(maxlen=256)
        self._terminal_turns_inflight: set[tuple[str, str]] = set()
        self._host: ToolHost | None = None
        self._started = False
        self._closing = False
        self._spawn_lock = asyncio.Lock()
        self.required_models = required_models
        self._engine_lock = asyncio.Lock()
        self._stop_task: asyncio.Task | None = None
        self.runtime_enabled = True
        self.async_tools = False
        self._distiller = None
        self._distiller_path: Path | None = None
        self._distiller_auto = False

    def configure_run(self, *, runtime_enabled: bool = True, async_tools: bool = False, log_distiller=None) -> None:
        """Controller-owned policy. Never configurable by a provider tool call."""
        if not isinstance(runtime_enabled, bool):
            raise ValueError("runtime_enabled must be a boolean")
        if not isinstance(async_tools, bool):
            raise ValueError("async_tools must be a boolean")
        config = log_distiller.to_json_data() if hasattr(log_distiller, "to_json_data") else (log_distiller or {})
        enabled = config.get("enabled", False)
        path = None
        automatic = enabled and not config.get("model_path")
        if enabled:
            value = config.get("model_path")
            if automatic:
                from supervisor.runtime.distiller import require_dependencies
                from supervisor.runtime.distiller_download import default_bundle_path
                require_dependencies()
                path = default_bundle_path().resolve()
            else:
                path = Path(value).expanduser()
                path = (self.cwd / path).resolve() if not path.is_absolute() else path.resolve()
        if self._started and (runtime_enabled != self.runtime_enabled or async_tools != self.async_tools
                              or path != self._distiller_path):
            raise AppServerError("run policy changes require stopping the runtime first")
        if path is not None and (path != self._distiller_path or self._distiller is None):
            from supervisor.runtime.distiller import LogDistiller, require_dependencies, validate_bundle
            try:
                if not automatic:
                    validate_bundle(path)
            except (OSError, ValueError) as exc:
                raise AppServerError(f"Cannot use log-distiller bundle {path}: {exc}") from exc
            require_dependencies()
            self._distiller = LogDistiller(path)
        elif path is None:
            self._distiller = None
        self.runtime_enabled, self._distiller_path = runtime_enabled, path
        self.async_tools = async_tools
        self._distiller_auto = automatic

    async def start(self, **_kwargs) -> None:
        if self._started:
            return
        if self._distiller_auto:
            from supervisor.runtime.distiller_download import ensure_default_bundle
            try:
                await asyncio.to_thread(ensure_default_bundle)
            except Exception as exc:
                raise AppServerError(
                    "Cannot prepare the published log-distiller model. Check the network/cache "
                    "or supply a local bundle with --distiller-model. " + str(exc)
                ) from exc
        if self._distiller_path is not None and self._distiller is None:
            self.configure_run(runtime_enabled=self.runtime_enabled,
                               async_tools=self.async_tools,
                               log_distiller={"enabled": True, "model_path": (
                                   None if self._distiller_auto else str(self._distiller_path))})
        self._closing = False
        self._stop_task = None
        self._journal = RuntimeJournal(self.state_dir)
        self._threads = self._journal.threads()
        for record in self._threads.values():
            if record.get("activeTurnId"):
                record["interruptedTurnId"] = record.pop("activeTurnId")
        for thread_id in self._threads:
            self._save(thread_id)
        self._host = ToolHost(self._journal, self._scope_for, self._approve, self._emit, self._delegate,
                              distill=self._distiller.distill if self._distiller else None)
        self._started = True

    async def initialize(self, *, timeout: float = 30) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return {"runtime": "bello", "protocolVersion": 1, "engines": ["codex", "pi", "claude-code"]}

    async def _engine(self, name: str):
        async with self._engine_lock:
            return await self._load_engine(name)

    async def _load_engine(self, name: str):
        if name in self._engines:
            return self._engines[name]
        if name == "pi":
            from supervisor.runtime.install import worker_command
            command = worker_command()
            backend = WorkerTransport(command, Path(command[1]).parent, emit=lambda raw: self._emit(raw, engine="pi"),
                                      tool=self._call_tool, on_error=lambda error: self._engine_failed("pi", error))
            await backend.start()
            try:
                await backend.request("initialize", {"stateDir": str(self.state_dir / "pi"),
                    "agentDir": os.environ.get("BELLO_PI_AGENT_DIR", str(Path.home() / ".pi" / "agent"))})
            except BaseException:
                await backend.stop()
                raise
        elif name == "codex":
            from supervisor.runtime.codex import CodexBackend
            command, manifest = None, None
            if self._distiller is not None or self.async_tools:
                from supervisor.runtime.native_codex_install import ensure_native_async, ensure_native_selection
                # Installation is not an app-server request and can take longer
                # than an RPC deadline on the first run. Never change global Codex.
                command, manifest = await asyncio.to_thread(
                    ensure_native_async if self.async_tools else ensure_native_selection)
            backend = CodexBackend(state_dir=self.state_dir / "codex",
                                   emit=lambda raw: self._emit(raw, engine="codex"),
                                   tool_handler=self._call_tool,
                                   on_error=lambda error: self._engine_failed("codex", error),
                                   distiller=self._distiller, command=command,
                                   selection_manifest=manifest)
            await backend.request("initialize", {})
        elif name == "claude-code":
            from supervisor.runtime.claude import ClaudeBackend
            backend = ClaudeBackend(state_dir=self.state_dir / "claude",
                                    emit=lambda raw: self._emit(raw, engine="claude-code"), tool_handler=self._call_tool)
            await backend.request("initialize", {})
        else:
            raise AppServerError(f"unsupported execution engine: {name}")
        self._engines[name] = backend
        return backend

    async def _engine_failed(self, engine: str, error: BaseException) -> None:
        affected = []
        for thread, record in self._threads.items():
            if record["engine"] == engine:
                if record.get("activeTurnId"):
                    record["interruptedTurnId"] = record.pop("activeTurnId")
                    self._save(thread)
                affected.append(thread)
        # This includes yielded commands that no longer have an active tool
        # callback, and children running through another execution engine.
        if self._host:
            results = await finish_cleanup(asyncio.gather(
                *(operation for thread in affected for operation in (
                    self._host.cancel_turn(thread), self._stop_children(thread),
                )), return_exceptions=True,
            ))
            if any(isinstance(result, BaseException) for result in results):
                error.add_note("One or more owned runtime cleanup operations failed; all affected turns were fenced.")
        await self._notify_transport_error(error)

    def _save(self, thread_id: str) -> None:
        assert self._journal is not None
        self._journal.save_thread(thread_id, self._threads[thread_id])

    def _record(self, thread_id: Any) -> dict[str, Any]:
        if not isinstance(thread_id, str) or thread_id not in self._threads:
            raise AppServerError("unknown Bello thread")
        return self._threads[thread_id]

    def _public_thread(self, thread_id: str) -> dict[str, Any]:
        record = self._record(thread_id)
        return {"id": thread_id, "cwd": record["cwd"], "model": record["qualifiedModel"],
                "reasoningEffort": record.get("effort"), "parentThreadId": record.get("parentThreadId"),
                "status": "shutdown" if record.get("closed") else "active" if record.get("activeTurnId") else "idle"}

    def _scope_for(self, thread_id: str, turn_id: str) -> ToolScope:
        record = self._record(thread_id)
        if (self._closing or record.get("closed") or record.get("activeTurnId") != turn_id
                or (thread_id, turn_id) in self._terminal_turns_inflight):
            raise AppServerError("tool request belongs to an inactive or stale turn")
        return ToolScope(root=Path(record["cwd"]), mode=record["sandbox"],
                         readable_roots=tuple(Path(p) for p in record.get("runtimeWorkspaceRoots", [])),
                         approval_policy=record.get("approvalPolicy", "on-request"),
                         network_access=record.get("networkAccess", False),
                         distiller_enabled=record.get("distillerEnabled", False),
                         runtime_enabled=self.runtime_enabled,
                         async_tools=record.get("asyncTools", False),
                         temp_root=Path(record["runtimeScratchRoot"]) if record.get("runtimeScratchRoot") else None,
                         task_path=Path(record["runtimeTaskPath"]) if record.get("runtimeTaskPath") else None)

    async def reconcile_terminal_turn(self, thread_id: str, turn_id: str,
                                      turn: dict[str, Any]) -> bool:
        """Apply a controller-verified native thread/read result without replaying work."""
        if (not self._started or self._closing or not isinstance(thread_id, str)
                or not isinstance(turn_id, str) or not turn_id or not isinstance(turn, dict)
                or turn.get("id") != turn_id
                or turn.get("status") not in ("completed", "failed", "interrupted")):
            return False
        record = self._threads.get(thread_id)
        if (record is None or record.get("engine") != "codex" or record.get("closed")
                or record.get("activeTurnId") != turn_id
                or (thread_id, turn_id) in self._terminal_turns_inflight):
            return False
        backend = self._engines.get("codex")
        reconcile = getattr(backend, "reconcile_terminal_turn", None)
        if not callable(reconcile):
            return False
        # The ordinary event path owns host cleanup before synchronously fencing
        # both identity layers. Cancellation during cleanup leaves this retryable.
        await self._emit({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": deepcopy(turn),
        }}, engine="codex", _reconcile_native=True)
        return record.get("lastTurnId") == turn_id and record.get("lastTurnStatus") == turn["status"]

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if not self._started:
            await self.start()
        params = deepcopy(params or {})
        # Sandbox networking is a run-level host policy, independent of the
        # provider's legacy offline defaults. Filesystem roots stay unchanged.
        if "sandboxPolicy" in params and not self.runtime_enabled:
            params["sandboxPolicy"]["networkAccess"] = True
        if method == "initialize":
            return await self.initialize(timeout=timeout)
        if method == "model/validate":
            selection = parse_model_selection(params["model"])
            backend = await self._engine(selection.engine)
            return await backend.request(method, {**params, "provider": selection.provider,
                                                   "model": selection.model,
                                                   "asyncTools": self.async_tools and params.get("belloRole") != "runtime"},
                                         timeout=timeout)
        if method in {"model/list", "account/read"}:
            requested_engines = params.pop("engines", None)
            optional = params.pop("optionalEngines", False)
            if requested_engines is not None and (not isinstance(requested_engines, list)
                    or not requested_engines or any(name not in {"codex", "pi", "claude-code"} for name in requested_engines)):
                raise AppServerError("engines must contain codex, pi and/or claude-code")
            engines = set(requested_engines) if requested_engines else {
                parse_model_selection(model).engine for model in self.required_models} or {"codex"}
            responses = []
            unavailable = {}
            for name in sorted(engines):
                try:
                    backend = await self._engine(name)
                    response = await backend.request(method, params, timeout=timeout)
                    if name == "pi" and method == "model/list":
                        # Pi can advertise its own Codex OAuth provider, but that
                        # route is no longer executable through Bello's Pi engine.
                        response["data"] = [item for item in response.get("data", [])
                            if item.get("provider") != "openai-codex"
                            and not str(item.get("qualifiedId", item.get("id", ""))).startswith("openai-codex/")]
                    responses.append(response)
                except Exception as exc:
                    if not optional:
                        raise
                    unavailable[name] = str(exc)
            if method == "model/list":
                data = [item for response in responses for item in response.get("data", [])]
                for item in list(data):
                    if isinstance(item, dict) and item.get("qualifiedId"):
                        data.append({**item, "id": item["qualifiedId"]})
                return {"data": data, **({"unavailableEngines": unavailable} if unavailable else {})}
            return {"accounts": responses, **({"unavailableEngines": unavailable} if unavailable else {})}
        if method == "account/rateLimits/read":
            if "codex" in self._engines or any(parse_model_selection(model).engine == "codex" for model in self.required_models):
                return await (await self._engine("codex")).request(method, params, timeout=timeout)
            return {"available": False, "reason": "Provider-specific quota data is not exposed by this runtime"}
        if method == "configRequirements/read":
            if "codex" in self._engines or any(parse_model_selection(model).engine == "codex" for model in self.required_models):
                return await (await self._engine("codex")).request(method, params, timeout=timeout)
            return {"requirements": {"managedTools": True, "protocolVersion": 1}}
        if method == "thread/list":
            return {"data": [self._public_thread(key) for key, value in self._threads.items() if not value.get("closed")]}
        if method == "thread/start":
            return await self._start_thread(params, timeout)
        thread_id = params.get("threadId")
        record = self._record(thread_id)
        if method in {"thread/resume", "turn/start"}:
            if parse_model_selection(record["qualifiedModel"]).engine != record["engine"]:
                raise AppServerError("Saved thread used the previous Pi subscription route; start a fresh run. "
                                     "Bello cannot migrate its conversation into native Codex silently.")
            role = record.get("belloRole", record.get("config", {}).get("agents", {}).get("role"))
            expected_distiller = self._distiller is not None and role == "coder"
            expected_async = self.async_tools and role != "runtime"
            if (record.get("networkAccess", False) != (not self.runtime_enabled)
                    or record.get("distillerEnabled", False) != expected_distiller
                    or record.get("asyncTools", False) != expected_async
                    or (params.get("belloRole") is not None and params["belloRole"] != role)):
                raise AppServerError(
                    "Saved thread uses a different runtime/distiller/async tool policy; start a fresh run "
                    "instead of resuming it with changed switches."
                )
        if not self.runtime_enabled:
            params["approvalPolicy"] = "never"
        engine = await self._engine(record["engine"])
        if method == "turn/start":
            if record.get("closed") or record.get("activeTurnId"):
                raise AppServerError("cannot start a turn on a closed or already active thread")
            selection = parse_model_selection(params.get("model", record["qualifiedModel"]))
            if selection.qualified != record["qualifiedModel"]:
                raise AppServerError("model changes require an explicitly created new thread")
            self._validate_scope_overrides(record, params)
            turn_id = str(uuid4())
            record["activeTurnId"] = turn_id
            record.pop("interruptedTurnId", None)
            effort = params.get("effort", record.get("effort"))
            self._save(thread_id)
            try:
                response = await engine.request(method, {**params, "turnId": turn_id, "effort": effort}, timeout=timeout)
                if response.get("turn", {}).get("id") != turn_id:
                    raise AppServerError("execution engine did not preserve the assigned turn identity")
                record["effort"] = effort
                self._save(thread_id)
                if record.get("lastTurnId") == turn_id:
                    response["turn"]["status"] = record["lastTurnStatus"]
                return response
            except BaseException:
                record.pop("activeTurnId", None)
                record["interruptedTurnId"] = turn_id
                self._save(thread_id)
                # The provider may have accepted a request whose reply was
                # lost. Fence tools first, then attempt to stop that exact
                # turn. Never replay turn/start as a recovery strategy.
                assert self._host
                await finish_cleanup(asyncio.gather(self._host.cancel_turn(thread_id), self._stop_children(thread_id),
                    engine.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5),
                    return_exceptions=True))
                raise
        if method == "turn/steer":
            if record.get("activeTurnId") != params.get("expectedTurnId"):
                raise AppServerError("cannot steer an inactive or stale turn")
        if method == "turn/interrupt":
            expected = params.get("turnId")
            if not expected or expected not in {record.get("activeTurnId"), record.get("interruptedTurnId")}:
                return {}
            record.pop("activeTurnId", None)
            record["interruptedTurnId"] = expected
            self._save(thread_id)
            assert self._host
            results = await finish_cleanup(asyncio.gather(self._host.cancel_turn(thread_id), self._stop_children(thread_id),
                engine.request(method, params, timeout=timeout), return_exceptions=True))
            self._raise_cleanup_errors(results, "turn interrupt")
            return results[-1]
        if method in {"thread/archive", "thread/unsubscribe"}:
            expected = record.pop("activeTurnId", None) or record.get("interruptedTurnId")
            if expected:
                record["interruptedTurnId"] = expected
            record["closed"] = True
            self._save(thread_id)
            assert self._host

            async def close_backend():
                failures = []
                if expected:
                    try:
                        await engine.request("turn/interrupt", {"threadId": thread_id, "turnId": expected}, timeout=timeout)
                    except Exception as exc:
                        failures.append(exc)
                # Archiving must still be attempted if interrupt failed.
                result = await engine.request(method, params, timeout=timeout)
                self._raise_cleanup_errors(failures, "backend interrupt before archive")
                return result

            results = await finish_cleanup(asyncio.gather(self._host.cancel_turn(thread_id), self._stop_children(thread_id),
                                          close_backend(), return_exceptions=True))
            self._raise_cleanup_errors(results, "thread archive")
            await self._emit({"method": "thread/closed", "params": {"threadId": thread_id}})
            return results[-1]
        if method == "thread/resume":
            self._validate_scope_overrides(record, params)
            if record.get("activeTurnId"):
                raise AppServerError("cannot resume a thread while its turn is active")
            params.update({"tools": record["tools"], "provider": record["provider"], "model": record["model"],
                           "asyncTools": record.get("asyncTools", False),
                           "developerInstructions": record.get("developerInstructions", "")})
        response = await engine.request(method, params, timeout=timeout)
        if method == "thread/resume":
            record["closed"] = False
            # Older saved threads predate the host-only task exclusion metadata.
            # Adopting the pinned task does not grant new filesystem authority.
            if not record.get("runtimeTaskPath") and params.get("runtimeTaskPath"):
                record["runtimeTaskPath"] = str(Path(params["runtimeTaskPath"]).resolve())
            self._save(thread_id)
        if method in {"thread/read", "thread/resume"} and isinstance(response.get("thread"), dict):
            response["thread"].update(self._public_thread(thread_id))
        return response

    async def _start_thread(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        selection = parse_model_selection(params.get("model", "gpt-5.6-sol"))
        root = Path(params.get("cwd", self.cwd)).resolve(strict=True)
        mode = params.get("sandbox", "read-only")
        if mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise AppServerError(f"unsupported sandbox mode: {mode}")
        if params.get("runtimeScratchRoot") is not None:
            scratch_policy = SandboxPolicy(root=root, mode=mode, temp_root=Path(params["runtimeScratchRoot"]))
            params["runtimeScratchRoot"] = str(scratch_policy.temp_root)
        if self.state_dir.resolve().is_relative_to(root):
            # The model must receive a disposable workspace, not the trusted
            # controller directory that stores its sessions and approvals.
            raise AppServerError("agent workspace contains Bello's private runtime state")
        thread_id = params.pop("threadId", str(uuid4()))
        if thread_id in self._threads:
            raise AppServerError("duplicate thread id")
        agents = params.get("config", {}).get("agents", {})
        enabled = bool(agents.get("enabled", False))
        role = params.get("belloRole", agents.get("role"))
        coder = role == "coder"
        distill = self._distiller is not None and coder
        async_mode = self.async_tools and role != "runtime"
        params["asyncTools"] = async_mode
        if async_mode:
            from supervisor.runtime.async_tools import ASYNC_TOOLS_GUIDANCE
            previous = params.get("developerInstructions") or ""
            # Cross-provider descendants inherit this exact block once.
            if ASYNC_TOOLS_GUIDANCE not in previous:
                params["developerInstructions"] = (previous + "\n" + ASYNC_TOOLS_GUIDANCE).strip()
        params["networkAccess"] = not self.runtime_enabled
        params["distillerEnabled"] = distill
        if not self.runtime_enabled:
            params["approvalPolicy"] = "never"
        if distill:
            from supervisor.runtime.codex_distiller import FOCUS_GUIDANCE
            if selection.engine == "codex":
                focus_instruction = FOCUS_GUIDANCE
            else:
                focus_instruction = "Add a very short focus to each text tool call."
            previous = params.get("developerInstructions") or ""
            # A cross-provider child inherits the parent instructions. Replace
            # only our exact generated focus line, never stack both variants.
            previous = "\n".join(line for line in previous.splitlines()
                                 if line not in {FOCUS_GUIDANCE, "Add a very short focus to each text tool call."})
            params["developerInstructions"] = (previous + "\n" + focus_instruction).strip()
        tools = [entry for entry in tool_definitions(distiller=distill, runtime_enabled=self.runtime_enabled,
                                                   async_tools=async_mode) if enabled or entry["name"] not in
                 {"spawn_agent", "send_message", "wait_agent", "close_agent"}]
        record = {**params, "cwd": str(root), "sandbox": mode, "engine": selection.engine,
                  "qualifiedModel": selection.qualified, "provider": selection.provider,
                  "model": selection.model, "tools": tools, "turns": [], "closed": False}
        self._threads[thread_id] = record
        self._save(thread_id)
        backend = None
        try:
            backend = await self._engine(selection.engine)
            response = await backend.request("thread/start", {**record, "threadId": thread_id}, timeout=timeout)
            if response.get("thread", {}).get("id") != thread_id:
                raise AppServerError("execution engine did not preserve the assigned thread identity")
            resolved_effort = response["thread"].get("reasoningEffort")
            if isinstance(resolved_effort, str):
                if record.get("effort") is not None and record["effort"] != resolved_effort:
                    raise AppServerError("execution engine changed the requested reasoning effort")
                record["effort"] = resolved_effort
                self._save(thread_id)
            response["thread"].update(self._public_thread(thread_id))
            sandbox = ({"type": "workspaceWrite", "networkAccess": record["networkAccess"], "writableRoots": [str(root)]}
                       if mode == "workspace-write" else {"type": "readOnly", "networkAccess": record["networkAccess"]}
                       if mode == "read-only" else {"type": "dangerFullAccess"})
            await self._emit({"method": "thread/started", "params": {"threadId": thread_id, "thread": self._public_thread(thread_id)}})
            return {**response, "approvalPolicy": record.get("approvalPolicy", "on-request"), "sandbox": sandbox}
        except BaseException as exc:
            record["closed"] = True
            self._save(thread_id)
            if backend is not None:
                # The engine may have created its session before losing the
                # acknowledgement. Do not replay creation or leave an orphan.
                try:
                    await finish_cleanup(backend.request("thread/archive", {"threadId": thread_id}, timeout=5))
                except Exception as cleanup_error:
                    exc.add_note(f"Closing the unacknowledged runtime thread failed: {type(cleanup_error).__name__}")
            raise

    def _validate_scope_overrides(self, record: dict[str, Any], params: dict[str, Any]) -> None:
        if "asyncTools" in params and params["asyncTools"] != record.get("asyncTools", False):
            raise AppServerError("a resumed thread cannot change its async tools policy")
        if "cwd" in params and Path(params["cwd"]).resolve() != Path(record["cwd"]):
            raise AppServerError("a turn cannot change the thread's assigned workspace")
        if "sandbox" in params and params["sandbox"] != record["sandbox"]:
            raise AppServerError("a resumed thread cannot change its sandbox")
        policy = params.get("sandboxPolicy", {})
        modes = {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}
        if policy and (modes.get(policy.get("type")) != record["sandbox"]
                       or policy.get("networkAccess", False) != record.get("networkAccess", False)):
            raise AppServerError("a turn cannot weaken the thread's sandbox policy")
        if "networkAccess" in params and params["networkAccess"] != record.get("networkAccess", False):
            raise AppServerError("a resumed thread cannot change network authority")
        if "belloRole" in params and params["belloRole"] != record.get("belloRole"):
            raise AppServerError("a resumed thread cannot change its role")
        if "runtimeScratchRoot" in params and params["runtimeScratchRoot"] != record.get("runtimeScratchRoot"):
            raise AppServerError("a resumed thread cannot change its temporary directory")
        if policy.get("writableRoots") is not None and {str(Path(p).resolve()) for p in policy["writableRoots"]} != {record["cwd"]}:
            raise AppServerError("a turn cannot add writable roots")
        roots = params.get("runtimeWorkspaceRoots")
        if roots is not None and {str(Path(p).resolve()) for p in roots} != {
            str(Path(p).resolve()) for p in record.get("runtimeWorkspaceRoots", [record["cwd"]])
        }:
            raise AppServerError("a turn cannot expand its assigned filesystem scope")

    async def _emit(self, raw: dict[str, Any], *, engine: str | None = None,
                    _reconcile_native: bool = False) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get("method"), str) or not isinstance(raw.get("params", {}), dict):
            raise AppServerError("execution engine sent an invalid event")
        message = AppServerMessage(raw)
        if engine is not None and message.method in {"thread/started", "thread/closed"}:
            # Thread creation is host-owned and announced once, after the
            # backend has acknowledged the assigned identity and workspace.
            return
        params = message.params
        thread_id = params.get("threadId")
        record = self._threads.get(thread_id)
        if thread_id is not None and (record is None or engine is not None and record["engine"] != engine):
            raise AppServerError("execution engine event has an unknown or foreign thread identity")
        turn = params.get("turn", {})
        if not isinstance(turn, dict):
            raise AppServerError("execution engine sent an invalid turn event")
        turn_id = params.get("turnId") or turn.get("id")
        if record and turn_id is not None and turn_id not in {
            record.get("activeTurnId"), record.get("interruptedTurnId")
        }:
            # Late events cannot alter readiness/validation for a later turn.
            return
        if record and turn_id is not None and turn_id == record.get("interruptedTurnId") and message.method != "turn/completed":
            return
        if record and message.method == "turn/completed":
            if turn.get("status") not in {"completed", "failed", "interrupted"}:
                raise AppServerError("execution engine sent an invalid terminal status")
            identity = (thread_id, turn_id)
            if identity in self._terminal_turns_inflight:
                return
            self._terminal_turns_inflight.add(identity)
            try:
                if self._host and turn_id:
                    await self._host.finish_turn(thread_id, turn_id)
                # Cleanup yields: an interruption/new turn may have superseded
                # this event. Never publish old readiness into the new turn.
                if turn_id not in {record.get("activeTurnId"), record.get("interruptedTurnId")}:
                    return
                if _reconcile_native:
                    # A paused/replaced turn must not be reconciled by a stale
                    # read result, even if ordinary interrupted events may finish.
                    if (engine != "codex" or self._closing or record.get("closed")
                            or record.get("activeTurnId") != turn_id):
                        return
                    reconcile = getattr(self._engines.get("codex"), "reconcile_terminal_turn", None)
                    if not callable(reconcile) or not reconcile(thread_id, turn_id, turn):
                        return
                if record.get("activeTurnId") == turn_id:
                    record.pop("activeTurnId", None)
                if record.get("interruptedTurnId") == turn_id:
                    record.pop("interruptedTurnId", None)
                record["lastTurnId"] = turn_id
                record["lastTurnStatus"] = turn["status"]
                self._save(thread_id)
            finally:
                self._terminal_turns_inflight.discard(identity)
        self._recent.append(message)
        await self._dispatch(message)

    async def wait_for_notification(self, predicate, *, timeout: float = 30) -> AppServerMessage:
        for message in reversed(self._recent):
            if predicate(message):
                return message
        return await super().wait_for_notification(predicate, timeout=timeout)

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._host is not None
        return await self._host.call(params)

    async def _approve(self, method: str, params: dict[str, Any]) -> bool:
        request_id = "approval-" + str(uuid4())
        raw = {"id": request_id, "method": method, "params": params}
        if params.pop("belloApprovalPolicy", None) == "never":
            # Independent read-only/runtime checks cannot ask for an escalation.
            # The existing local policy remains authoritative; no model is added.
            resolution = await ApprovalManager(Path(params["cwd"])).decide(normalize_approval_request(AppServerMessage(raw)))
            return resolution.decision in ("accept", "acceptForSession")
        if self.server_request_handler is None:
            return False
        future = asyncio.get_running_loop().create_future()
        self._approval_waiters[request_id] = future
        try:
            await self._dispatch(AppServerMessage(raw))
            response = await asyncio.wait_for(future, 360)
            return response.get("decision") in ("accept", "acceptForSession")
        except asyncio.TimeoutError:
            return False
        finally:
            self._approval_waiters.pop(request_id, None)
            await self._emit({"method": "serverRequest/resolved", "params": {"requestId": request_id}})

    async def respond(self, request_id, result=None, *, error=None, timeout=15) -> None:
        future = self._approval_waiters.get(request_id)
        if future and not future.done():
            future.set_result(result if error is None and isinstance(result, dict) else {"decision": "decline"})
            return
        native = self._engines.get("codex")
        if native is not None and isinstance(request_id, str) and request_id.startswith("codex:"):
            await native.respond(request_id, result, error=error, timeout=timeout)

    async def _delegate(self, name: str, args: dict[str, Any], parent: str, turn_id: str) -> dict[str, Any]:
        self._scope_for(parent, turn_id)
        record = self._record(parent)
        agents = record.get("config", {}).get("agents", {})
        if not agents.get("enabled"):
            raise AppServerError("subagents are disabled for this role")
        if name == "spawn_agent":
            async with self._spawn_lock:
                self._scope_for(parent, turn_id)
                allowed = agents.get("allowed_profiles", {})
                selected = parse_model_selection(args["model"])
                match = next((efforts for model, efforts in allowed.items()
                              if parse_model_selection(model).qualified == selected.qualified), ())
                if args["effort"] not in match:
                    raise AppServerError("child model/effort is outside the allowed profile map")
                role = agents.get("role", "coder")
                if record.get("parentThreadId") and role != "coder":
                    raise AppServerError("nested delegation is disabled")
                family = record.get("rootThreadId", parent)
                depth = record.get("depth", 0)
                if depth >= 32:
                    raise AppServerError("subagent lineage depth limit reached")
                children = [r for r in self._threads.values() if r.get("rootThreadId") == family
                            and r.get("activeTurnId") and not r.get("closed")]
                if len(children) >= agents.get("max_concurrent_threads_per_session", 1):
                    raise AppServerError("subagent concurrency limit reached; wait for an active child first")
                child_params = {key: deepcopy(record[key]) for key in (
                    "cwd", "sandbox", "approvalPolicy", "runtimeWorkspaceRoots", "runtimeTaskPath", "runtimeScratchRoot", "serviceTier", "belloRole"
                ) if key in record}
                child_params.update(model=selected.qualified, effort=args["effort"], parentThreadId=parent,
                                    rootThreadId=family, depth=depth + 1,
                                    developerInstructions=record.get("developerInstructions", ""),
                                    config={"agents": deepcopy(agents) if role == "coder" else {"enabled": False}})
                response = await self._start_thread(child_params, 30)
                child = response["thread"]["id"]
                try:
                    self._scope_for(parent, turn_id)
                    await self.request("turn/start", {"threadId": child, "input": [{"type": "text", "text": args["message"]}], "effort": args["effort"]})
                except BaseException as exc:
                    try:
                        await self.request("thread/archive", {"threadId": child}, timeout=5)
                    except Exception as cleanup_error:
                        exc.add_note(f"Closing the failed child failed: {type(cleanup_error).__name__}")
                    raise
                return tool_result(json.dumps({"agent_id": child}))
        child = args["agent_id"]
        child_record = self._record(child)
        if child_record.get("parentThreadId") != parent:
            raise AppServerError("an agent may only control its own children")
        if name == "close_agent":
            await self.request("thread/archive", {"threadId": child})
            return tool_result("Child stopped and closed.")
        if name == "send_message":
            async with self._spawn_lock:
                self._scope_for(parent, turn_id)
                if child_record.get("closed"):
                    raise AppServerError("cannot send a message to a closed child")
                if child_record.get("activeTurnId"):
                    response = await self.turn_steer(child, child_record["activeTurnId"], args["message"])
                else:
                    family = record.get("rootThreadId", parent)
                    active = sum(bool(item.get("activeTurnId")) and not item.get("closed")
                                 for item in self._threads.values() if item.get("rootThreadId") == family)
                    if active >= agents.get("max_concurrent_threads_per_session", 1):
                        raise AppServerError("subagent concurrency limit reached; wait for an active child first")
                    response = await self.request("turn/start", {"threadId": child, "input": [{"type": "text", "text": args["message"]}]})
            return tool_result(json.dumps(response))
        if child_record.get("activeTurnId"):
            expected = child_record["activeTurnId"]
            while child_record.get("activeTurnId") == expected:
                try:
                    await self.wait_for_notification(lambda m: m.method == "turn/completed" and m.params.get("threadId") == child
                        and m.params.get("turn", {}).get("id") == expected, timeout=args.get("timeout", 60)
                        if not record.get("asyncTools", False) else 60)
                    break
                except AppServerTimeoutError:
                    if not record.get("asyncTools", False):
                        return tool_result("Child is still working.")
                    self._scope_for(parent, turn_id)
        response = await self.request("thread/read", {"threadId": child, "includeTurns": True})
        turns = response.get("thread", {}).get("turns", [])
        latest = turns[-1] if turns else {}
        findings = [item.get("text", "") for item in latest.get("items", []) if item.get("type") == "agentMessage"]
        if record.get("asyncTools", False):
            # Durable parent-owned delivery cursor: repeated waits must not
            # append the same completed child messages to the LLM history.
            deliveries = record.setdefault("childDeliveries", {})
            delivered = deliveries.get(child, {})
            child_turn = latest.get("id") or child_record.get("lastTurnId")
            # A new parent turn may be recovery after interruption before the
            # previous tool response reached its engine. Replay once there;
            # never lose a finding by treating dispatch as a delivery ACK.
            count = delivered.get("count", 0) if (
                delivered.get("turnId") == child_turn and delivered.get("parentTurnId") == turn_id) else 0
            total = len(findings)
            findings = findings[count:]
            deliveries[child] = {"turnId": child_turn, "parentTurnId": turn_id, "count": total}
            self._save(parent)
        return tool_result(json.dumps({"agent_id": child, "status": latest.get("status", "idle"),
                                       "messages": findings, "error": latest.get("error")}, ensure_ascii=False))

    async def _stop_children(self, thread_id: str) -> None:
        children = [child for child, record in list(self._threads.items())
                    if record.get("parentThreadId") == thread_id and not record.get("closed")]
        results = await asyncio.gather(*(self.request("thread/archive", {"threadId": child}) for child in children),
                                       return_exceptions=True)
        self._raise_cleanup_errors(results, "child cleanup")

    @staticmethod
    def _raise_cleanup_errors(results, operation: str) -> None:
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise AppServerError(f"{operation} failed for {len(failures)} operation(s); all cleanup attempts were made") from failures[0]

    async def stop(self, **_kwargs) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await finish_cleanup(self._stop_task)

    async def _stop(self) -> None:
        self._closing = True
        for thread, record in self._threads.items():
            if record.get("activeTurnId"):
                record["interruptedTurnId"] = record.pop("activeTurnId")
                self._save(thread)
        for future in self._approval_waiters.values():
            if not future.done():
                future.set_result({"decision": "decline"})
        engines = list(self._engines)
        cleanup = [backend.stop() for backend in self._engines.values()]
        if self._host:
            engines.append("tool host")
            cleanup.append(self._host.close())
        if self._distiller:
            engines.append("log distiller")
            cleanup.append(self._distiller.close())
        results = await asyncio.gather(*cleanup, return_exceptions=True)
        self._distiller = None
        self._engines.clear()
        if self._journal:
            self._journal.close()
            self._journal = None
        self._started = False
        failed = [name for name, result in zip(engines, results) if isinstance(result, BaseException)]
        if failed:
            raise AppServerError(f"Runtime cleanup failed for {', '.join(failed)}; active turns remain fenced")

    async def restart(self) -> None:
        await self.stop()
        await self.start()
