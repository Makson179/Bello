"""Provider-neutral controller boundary with Bello-owned session/tool scope.

The thread/turn/item names are an internal compatibility contract for Bello's
existing safety logic. This client never starts Codex app-server.
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
from supervisor.runtime.tools import TOOL_DEFINITIONS, ToolHost, ToolScope, tool_result
from supervisor.runtime.transport import WorkerTransport


class RuntimeClient(AppServerClient):
    """Reuse the established typed convenience methods, not the old executor."""

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
        self._host: ToolHost | None = None
        self._started = False
        self._closing = False
        self._spawn_lock = asyncio.Lock()
        self.required_models = required_models
        self._engine_lock = asyncio.Lock()
        self._stop_task: asyncio.Task | None = None

    async def start(self, **_kwargs) -> None:
        if self._started:
            return
        self._closing = False
        self._stop_task = None
        self._journal = RuntimeJournal(self.state_dir)
        self._threads = self._journal.threads()
        for record in self._threads.values():
            if record.get("activeTurnId"):
                record["interruptedTurnId"] = record.pop("activeTurnId")
        for thread_id in self._threads:
            self._save(thread_id)
        self._host = ToolHost(self._journal, self._scope_for, self._approve, self._emit, self._delegate)
        self._started = True

    async def initialize(self, *, timeout: float = 30) -> dict[str, Any]:
        if not self._started:
            await self.start()
        return {"runtime": "bello", "protocolVersion": 1, "engines": ["pi", "claude-code"]}

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
        if self._closing or record.get("closed") or record.get("activeTurnId") != turn_id:
            raise AppServerError("tool request belongs to an inactive or stale turn")
        return ToolScope(root=Path(record["cwd"]), mode=record["sandbox"],
                         readable_roots=tuple(Path(p) for p in record.get("runtimeWorkspaceRoots", [])),
                         approval_policy=record.get("approvalPolicy", "on-request"), network_access=False)

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if not self._started:
            await self.start()
        params = deepcopy(params or {})
        if method == "initialize":
            return await self.initialize(timeout=timeout)
        if method == "model/validate":
            selection = parse_model_selection(params["model"])
            backend = await self._engine(selection.engine)
            return await backend.request(method, {**params, "provider": selection.provider,
                                                   "model": selection.model}, timeout=timeout)
        if method in {"model/list", "account/read"}:
            requested_engines = params.pop("engines", None)
            optional = params.pop("optionalEngines", False)
            if requested_engines is not None and (not isinstance(requested_engines, list)
                    or not requested_engines or any(name not in {"pi", "claude-code"} for name in requested_engines)):
                raise AppServerError("engines must contain pi and/or claude-code")
            engines = set(requested_engines) if requested_engines else {
                parse_model_selection(model).engine for model in self.required_models} or {"pi"}
            responses = []
            unavailable = {}
            for name in sorted(engines):
                try:
                    backend = await self._engine(name)
                    responses.append(await backend.request(method, params, timeout=timeout))
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
            return {"available": False, "reason": "Provider-specific quota data is not exposed by this runtime"}
        if method == "configRequirements/read":
            return {"requirements": {"managedTools": True, "protocolVersion": 1}}
        if method == "thread/list":
            return {"data": [self._public_thread(key) for key, value in self._threads.items() if not value.get("closed")]}
        if method == "thread/start":
            return await self._start_thread(params, timeout)
        thread_id = params.get("threadId")
        record = self._record(thread_id)
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
            params.update({"tools": record["tools"], "provider": record["provider"], "model": record["model"]})
        response = await engine.request(method, params, timeout=timeout)
        if method == "thread/resume":
            record["closed"] = False
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
        if self.state_dir.resolve().is_relative_to(root):
            # The model must receive a disposable workspace, not the trusted
            # controller directory that stores its sessions and approvals.
            raise AppServerError("agent workspace contains Bello's private runtime state")
        thread_id = params.pop("threadId", str(uuid4()))
        if thread_id in self._threads:
            raise AppServerError("duplicate thread id")
        agents = params.get("config", {}).get("agents", {})
        enabled = bool(agents.get("enabled", False))
        tools = [entry for entry in TOOL_DEFINITIONS if enabled or entry["name"] not in
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
            sandbox = ({"type": "workspaceWrite", "networkAccess": False, "writableRoots": [str(root)]}
                       if mode == "workspace-write" else {"type": "readOnly", "networkAccess": False}
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
        if "cwd" in params and Path(params["cwd"]).resolve() != Path(record["cwd"]):
            raise AppServerError("a turn cannot change the thread's assigned workspace")
        if "sandbox" in params and params["sandbox"] != record["sandbox"]:
            raise AppServerError("a resumed thread cannot change its sandbox")
        policy = params.get("sandboxPolicy", {})
        modes = {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}
        if policy and (modes.get(policy.get("type")) != record["sandbox"] or policy.get("networkAccess", False)):
            raise AppServerError("a turn cannot weaken the thread's sandbox policy")
        if policy.get("writableRoots") is not None and {str(Path(p).resolve()) for p in policy["writableRoots"]} != {record["cwd"]}:
            raise AppServerError("a turn cannot add writable roots")
        roots = params.get("runtimeWorkspaceRoots")
        if roots is not None and {str(Path(p).resolve()) for p in roots} != {
            str(Path(p).resolve()) for p in record.get("runtimeWorkspaceRoots", [record["cwd"]])
        }:
            raise AppServerError("a turn cannot expand its assigned filesystem scope")

    async def _emit(self, raw: dict[str, Any], *, engine: str | None = None) -> None:
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
            if self._host and turn_id:
                await self._host.finish_turn(thread_id, turn_id)
            if record.get("activeTurnId") == turn_id:
                record.pop("activeTurnId", None)
            if record.get("interruptedTurnId") == turn_id:
                record.pop("interruptedTurnId", None)
            record["lastTurnId"] = turn_id
            record["lastTurnStatus"] = turn["status"]
            self._save(thread_id)
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
                    "cwd", "sandbox", "approvalPolicy", "runtimeWorkspaceRoots", "serviceTier"
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
            try:
                await self.wait_for_notification(lambda m: m.method == "turn/completed" and m.params.get("threadId") == child
                    and m.params.get("turn", {}).get("id") == expected, timeout=args.get("timeout", 60))
            except AppServerTimeoutError:
                return tool_result("Child is still working.")
        response = await self.request("thread/read", {"threadId": child, "includeTurns": True})
        turns = response.get("thread", {}).get("turns", [])
        latest = turns[-1] if turns else {}
        findings = [item.get("text", "") for item in latest.get("items", []) if item.get("type") == "agentMessage"]
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
        results = await asyncio.gather(*cleanup, return_exceptions=True)
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
