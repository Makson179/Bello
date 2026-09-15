"""Native subscription Codex engine, without replacing its prompt or tool host.

Bello owns public thread/turn identities; Codex owns its persisted conversations,
native tools and agent loop. Only configured cross-engine delegation uses dynamic
tools. Notifications are queued away from the app-server reader, so replies and
approval RPCs cannot deadlock that reader. Uncertain turns are never replayed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import OrderedDict
import inspect
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from supervisor.appserver import (AppServerClient, AppServerError, AppServerMessage,
    AppServerTimeoutError, _private_owned_directory)
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.codex_permissions import native_permission_params
from supervisor.runtime.codex_toolchains import native_toolchain_read_paths


_DELEGATION = frozenset({"spawn_agent", "send_message", "wait_agent", "close_agent"})
_THREAD_FIELDS = frozenset({"cwd", "approvalPolicy", "approvalsReviewer", "sandbox", "personality",
    "serviceTier", "ephemeral", "experimentalRawEvents", "persistExtendedHistory", "developerInstructions"})
_TURN_FIELDS = frozenset({"input", "cwd", "approvalPolicy", "approvalsReviewer", "sandboxPolicy",
    "effort", "summary", "outputSchema", "serviceTier", "personality"})
_APPROVALS = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "execCommandApproval", "applyPatchApproval"})
_IDENTITY_FIELDS = {"threadId", "parentThreadId", "senderThreadId", "receiverThreadId"}


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise AppServerError(f"Codex {name} must be a nonempty identifier")
    return value


def _model(value: Any) -> str:
    value = _identifier(value, "model")
    if value.startswith("openai-codex/"):
        value = value.split("/", 1)[1]
    if "/" in value or any(c.isspace() for c in value):
        raise AppServerError("native Codex accepts only an exact openai-codex subscription model")
    return value


def _effort(params: dict[str, Any]) -> str | None:
    config = params.get("config") or {}
    return params.get("effort") or config.get("model_reasoning_effort")


def _tier(value: Any) -> str:
    return "fast" if value in {"priority", "fast"} else value or "default"


class CodexBackend:
    def __init__(self, *, state_dir: Path, emit, tool_handler=None, client_factory=None,
                 on_error=None, command: list[str] | None = None, distiller=None):
        self.state_dir = Path(state_dir).absolute()
        self.emit, self.tool_handler, self.on_error = emit, tool_handler, on_error
        self._factory = client_factory or AppServerClient
        self._command = list(command) if command else None
        self._distiller = distiller
        self._bridge = None
        self._selection_verified = False
        self._selection_manifest: Path | None = None
        self._selection_lock = asyncio.Lock()
        self._native_command = None
        self._runtime_read_paths: tuple[Path, ...] = ()
        self._toolchain_read_paths: dict[str, tuple[Path, ...]] = {}
        self._client = None
        self._journal = RuntimeJournal(self.state_dir)
        self._tool_tmp = self.state_dir / "codex-tmp"
        self._tool_tmp.mkdir(mode=0o700, exist_ok=True)
        _private_owned_directory(self._tool_tmp)
        self._threads = self._journal.threads()
        self._native_threads: dict[str, str] = {}
        self._native_turns: dict[tuple[str, str], str] = {}
        for host_id, record in self._threads.items():
            if record.get("id") != host_id or not isinstance(record.get("nativeId"), str):
                raise AppServerError("invalid persisted native Codex identity mapping")
            self._native_threads[record["nativeId"]] = host_id
            for native, host in record.get("turnIds", {}).items():
                self._native_turns[(record["nativeId"], native)] = host
            if record.pop("activeTurnId", None):
                self._journal.save_thread(host_id, record)
        self._loaded: set[str] = set()
        self._catalog = None
        self._auth = None
        self._closing = False
        self._initialized = False
        self._failure: BaseException | None = None
        self._start_lock = asyncio.Lock()
        self._thread_start_lock = asyncio.Lock()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._pending_turns: dict[str, str] = {}
        self._starting_thread = False
        self._deferred: list[dict[str, Any]] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pump = None
        self._tasks: set[asyncio.Task] = set()
        self._requests: dict[str, int | str] = {}
        self._request_history: OrderedDict[int | str, str] = OrderedDict()

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if timeout <= 0:
            raise AppServerTimeoutError("native Codex request deadline expired before dispatch")
        try:
            async with asyncio.timeout(timeout):
                await self._ensure_initialized(timeout)
                return await self._request(method, deepcopy(params or {}), timeout)
        except TimeoutError as exc:
            raise AppServerTimeoutError(f"native Codex {method} timed out; uncertain actions are not replayed") from exc

    async def _ensure_initialized(self, timeout: float) -> None:
        if self._closing:
            raise AppServerError("native Codex backend is stopped")
        if self._failure is not None:
            raise AppServerError("native Codex transport failed; explicitly resume with a new backend") from self._failure
        if self._initialized:
            return
        async with self._start_lock:
            if self._initialized:
                return
            environment = {"OPENAI_API_KEY": None, "CODEX_API_KEY": None, "OPENAI_BASE_URL": None}
            environment.update({key: str(self._tool_tmp) for key in ("TMPDIR", "TMP", "TEMP")})
            # Git must work without opening the user's private global config
            # (or invoking its credential helpers) outside the assigned scope.
            environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            command = self._command or [os.environ.get("BELLO_CODEX_BINARY", "codex"), "app-server", "--listen", "stdio://"]
            self._native_command = list(command)
            # Deployment-owned capability files are host settings, not thread
            # configuration and not instructions supplied by the coder.
            manifest = os.environ.get("BELLO_CODEX_SELECTION_MANIFEST", "").strip()
            self._selection_manifest = Path(manifest).expanduser().absolute() if manifest else None
            executable = shutil.which(command[0])
            if executable is not None:
                launcher = Path(executable).absolute()
                self._runtime_read_paths = tuple(dict.fromkeys((launcher, launcher.resolve(strict=True))))
            if self._distiller is not None:
                from supervisor.runtime.codex_distiller import CodexDistillerBridge
                self._bridge = CodexDistillerBridge(self._distiller, state_dir=self.state_dir / "selector")
                await self._bridge.start()
                environment.update(self._bridge.environment)
            # Overrides apply only to the child, never the user's global config.
            command = [*command, "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
                       "-c", "features.multi_agent=false"]
            self._client = self._factory(command=command, cwd=self.state_dir,
                notification_handler=self._receive, server_request_handler=self._receive,
                transport_error_handler=self._transport_error, environment_overrides=environment,
                persistent_isolated_home=self.state_dir / "codex-home")
            self._pump = asyncio.create_task(self._events())
            try:
                await self._client.start()
                await self._client.initialize(timeout=timeout)
                self._auth = await self._subscription(timeout)
                self._initialized = True
            except BaseException:
                await self.stop()
                raise

    async def _subscription(self, timeout: float) -> dict[str, Any]:
        response = await self._client.request("account/read", {"refreshToken": False}, timeout=timeout)
        account = response.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise AppServerError("native Codex requires a ChatGPT subscription login; API-key fallback is forbidden")
        return {"provider": "openai-codex", "billingRoute": "subscription", "authMethod": "chatgpt",
                **({"planType": account["planType"]} if isinstance(account.get("planType"), str) else {})}

    async def _models(self, timeout: float) -> list[dict[str, Any]]:
        if self._catalog is None:
            catalog, cursor, seen = [], None, set()
            while True:
                params = {"includeHidden": True, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                response = await self._client.request("model/list", params, timeout=timeout)
                for raw in response.get("data", []):
                    model = _model(raw.get("model") or raw.get("id"))
                    efforts = raw.get("supportedReasoningEfforts", [])
                    normalized = [e.get("reasoningEffort") if isinstance(e, dict) else e for e in efforts]
                    catalog.append({**deepcopy(raw), "id": model, "model": model, "provider": "openai-codex",
                        "qualifiedId": "openai-codex/" + model, "engine": "codex", "billingRoute": "subscription",
                        "supportedReasoningEfforts": [e for e in normalized if isinstance(e, str)],
                        "supportedEfforts": [e for e in normalized if isinstance(e, str)]})
                cursor = response.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise AppServerError("native Codex model catalog cursor repeated")
                seen.add(cursor)
            self._catalog = catalog
        return deepcopy(self._catalog)

    async def _validate(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if params.get("provider", "openai-codex") != "openai-codex":
            raise AppServerError("native Codex cannot change the requested provider/billing route")
        model = _model(params.get("model"))
        descriptor = next((m for m in await self._models(timeout) if m["id"] == model), None)
        if descriptor is None:
            raise AppServerError(f"native Codex did not advertise model {model!r}; no substitution is allowed")
        effort = _effort(params)
        if effort is not None and effort not in descriptor["supportedReasoningEfforts"]:
            raise AppServerError(f"native Codex model {model!r} does not support effort {effort!r}")
        if params.get("serviceTier") not in (None, "default", "priority", "fast"):
            raise AppServerError("unsupported native Codex service tier")
        if params.get("distillerEnabled"):
            await self._validate_selection()
        return {"valid": True, "model": descriptor,
                "requested": {"effort": effort, "serviceTier": params.get("serviceTier")},
                "execution": {"engine": "codex", "effort": effort}}

    async def _validate_selection(self) -> None:
        if self._bridge is None:
            raise AppServerError("native log distiller requested without a native selection bridge")
        async with self._selection_lock:
            if not self._selection_verified:
                from supervisor.runtime.codex_distiller import validate_native_selection
                await validate_native_selection(
                    self._native_command, manifest_path=self._selection_manifest,
                )
                self._selection_verified = True

    async def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if method == "initialize":
            return {"engine": "codex", "protocolVersion": 1, "nativeTools": True}
        if method == "account/read":
            return await self._subscription(timeout)
        if method == "model/list":
            return {"data": await self._models(timeout)}
        if method == "model/validate":
            return await self._validate(params, timeout)
        if method in {"account/rateLimits/read", "configRequirements/read"}:
            return await self._client.request(method, params, timeout=timeout)
        if method == "thread/start":
            return await self._thread_start(params, timeout)
        if method == "thread/list":
            return {"data": [self._public(r) for r in self._threads.values() if not r.get("closed")]}
        host = _identifier(params.get("threadId"), "threadId")
        record = self._threads.get(host)
        if record is None:
            raise AppServerError("unknown native Codex thread")
        if method == "turn/start":
            return await self._turn_start(record, params, timeout)
        if method == "thread/resume":
            async with self._lock(host):
                return await self._resume(record, params, timeout)
        if method == "thread/read":
            raw = await self._client.request(method, {"threadId": record["nativeId"],
                "includeTurns": params.get("includeTurns", True)}, timeout=timeout)
            return self._rewrite(raw, record["nativeId"])
        if method in {"thread/turns/list", "thread/items/list"}:
            forwarded = {k: v for k, v in params.items() if k in {"threadId", "turnId", "cursor", "limit", "itemsView", "sortDirection"}}
        elif method in {"thread/archive", "thread/unsubscribe"}:
            forwarded = {"threadId": host}
        elif method == "turn/interrupt":
            host_turn = params.get("turnId")
            if host_turn not in record.get("turnIds", {}).values():
                return {}
            forwarded = {"threadId": host, "turnId": host_turn}
        elif method == "turn/steer":
            forwarded = {k: v for k, v in params.items() if k in {"threadId", "expectedTurnId", "input"}}
        else:
            raise AppServerError(f"unsupported native Codex method: {method}")
        forwarded = self._to_native(record, forwarded)
        try:
            response = await self._client.request(method, forwarded, timeout=timeout)
        except AppServerError as exc:
            missing_rollout = str({"code": -32600,
                "message": f"no rollout found for thread id {record['nativeId']}"})
            if (method != "thread/archive" or record.get("turnIds") or record.get("activeTurnId")
                    or str(exc) != missing_rollout):
                raise
            # Native Codex does not persist a rollout until the first turn.
            # Release only this known, owned, never-started native subscription.
            response = await self._client.request("thread/unsubscribe",
                {"threadId": record["nativeId"]}, timeout=timeout)
        if method in {"thread/archive", "thread/unsubscribe"}:
            record["closed"] = True
            self._loaded.discard(host)
            self._save(record)
        return self._rewrite(response, record["nativeId"])

    def _lock(self, host: str) -> asyncio.Lock:
        return self._thread_locks.setdefault(host, asyncio.Lock())

    def _thread_params(self, params: dict[str, Any]) -> dict[str, Any]:
        model = _model(params.get("model"))
        if params.get("provider", "openai-codex") != "openai-codex" or params.get("baseInstructions") is not None:
            raise AppServerError("native Codex retains its native provider and base instructions")
        config = deepcopy(params.get("config") or {})
        config.pop("agents", None)  # Bello's profile schema is not Codex's agent config.
        for key in list(config):
            if key == "model_providers" or key.startswith("model_providers."):
                raise AppServerError("native Codex provider overrides are not permitted")
        config.update({"model_provider": "openai", "forced_login_method": "chatgpt", "features.multi_agent": False})
        if isinstance(config.get("features"), dict):
            config["features"]["multi_agent"] = False
        if _effort(params) is not None:
            config["model_reasoning_effort"] = _effort(params)
        if params.get("distillerEnabled"):
            if self._bridge is None or not self._selection_verified:
                raise AppServerError("native log distiller requested without a verified native selection bridge")
            self._bridge.register_scope(Path(params["cwd"]), Path(params["runtimeTaskPath"]) if params.get("runtimeTaskPath") else None)
            config.update(self._bridge.thread_config)
        elif self._selection_verified:
            config["features.bello_native_selection"] = False
        else:
            config.pop("features.bello_native_selection", None)
            if isinstance(config.get("features"), dict):
                config["features"].pop("bello_native_selection", None)
        allowed = [t for t in params.get("tools", []) if t.get("name") in _DELEGATION]
        agents_enabled = bool((params.get("config") or {}).get("agents", {}).get("enabled"))
        if not agents_enabled:
            allowed = []
        dynamic = [{"name": "bello_" + t["name"], "description": t["description"], "inputSchema": deepcopy(t["parameters"])} for t in allowed]
        native = {key: deepcopy(params[key]) for key in _THREAD_FIELDS if key in params}
        # Null means native default; use explicit default for usual/non-Fast runs.
        native["serviceTier"] = _tier(params.get("serviceTier"))
        if dynamic:
            mapping = "Use Bello's configured delegation tools: " + ", ".join(t["name"] for t in dynamic) + "."
            native["developerInstructions"] = ((native.get("developerInstructions") or "") + "\n" + mapping).strip()
        native.update(model=model, modelProvider="openai", config=config, dynamicTools=dynamic)
        toolchain_paths: tuple[Path, ...] = ()
        if params.get("sandbox", "workspace-write") != "danger-full-access":
            cwd = params.get("cwd")
            if isinstance(cwd, str) and Path(cwd).is_absolute():
                if cwd not in self._toolchain_read_paths:
                    self._toolchain_read_paths[cwd] = native_toolchain_read_paths(Path(cwd))
                toolchain_paths = self._toolchain_read_paths[cwd]
        permissions = native_permission_params(params, temp_dir=self._tool_tmp,
            runtime_read_paths=(*self._runtime_read_paths, *toolchain_paths))
        if "permissions" in permissions:
            native.pop("sandbox", None)
            config.pop("sandbox_workspace_write.network_access", None)
            if isinstance(config.get("sandbox_workspace_write"), dict):
                config["sandbox_workspace_write"].pop("network_access", None)
            config.update(permissions.pop("config"))
        native.update(permissions)
        return native

    @staticmethod
    def _confirm_profile(response: dict[str, Any], params: dict[str, Any]) -> None:
        expected_model = _model(params["model"])
        actual_model = response.get("model")
        if actual_model is not None and actual_model != expected_model:
            raise AppServerError("native Codex selected a different model than requested")
        if response.get("modelProvider") not in (None, "openai"):
            raise AppServerError("native Codex selected a different provider than requested")
        expected_effort = _effort(params)
        actual_effort = response.get("reasoningEffort", response.get("thread", {}).get("reasoningEffort"))
        if expected_effort is not None and actual_effort is not None and actual_effort != expected_effort:
            raise AppServerError("native Codex selected a different reasoning effort than requested")
        if "serviceTier" in response and _tier(response["serviceTier"]) != _tier(params.get("serviceTier")):
            raise AppServerError("native Codex selected a different service tier than requested")
        if params.get("sandbox", "workspace-write") != "danger-full-access" and "activePermissionProfile" in response:
            active = response["activePermissionProfile"]
            if not isinstance(active, dict) or active.get("id") != "bello-native" or active.get("extends") is not None:
                raise AppServerError("native Codex selected a different filesystem permission profile than requested")
        if actual_effort is not None:
            response["thread"]["reasoningEffort"] = actual_effort

    async def _thread_start(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        host = _identifier(params.get("threadId"), "threadId")
        async with self._thread_start_lock:
            if host in self._threads:
                raise AppServerError("duplicate native Codex thread id")
            await self._subscription(timeout)
            await self._validate(params, timeout)
            forwarded = self._thread_params(params)
            self._starting_thread = True
            try:
                response = await self._client.request("thread/start", forwarded, timeout=timeout)
                self._confirm_profile(response, params)
                native = _identifier(response.get("thread", {}).get("id"), "native threadId")
                if native in self._native_threads:
                    raise AppServerError("native Codex reused an owned thread identity")
                record = {"id": host, "nativeId": native, "turnIds": {}, "closed": False,
                    "params": deepcopy(params), "model": _model(params["model"]), "cwd": params["cwd"]}
                self._threads[host] = record
                self._native_threads[native] = host
                self._loaded.add(host)
                self._save(record)
                await self._flush_deferred()
                response = self._rewrite(response, native)
                return response
            except BaseException:
                # Thread creation may have reached Codex even when its reply was lost.
                await self._fail(AppServerError("native thread creation failed; no automatic replay"))
                raise
            finally:
                self._starting_thread = False

    async def _resume(self, record: dict[str, Any], params: dict[str, Any], timeout: float) -> dict[str, Any]:
        combined = {**record["params"], **params}
        if _model(combined["model"]) != record["model"] or str(Path(combined["cwd"]).resolve()) != str(Path(record["cwd"]).resolve()):
            raise AppServerError("native resume cannot change the assigned model or workspace")
        await self._subscription(timeout)
        await self._validate(combined, timeout)
        forwarded = {**self._thread_params(combined), "threadId": record["nativeId"]}
        # Codex resumes persisted dynamic tools; thread/resume does not take their schema.
        forwarded.pop("dynamicTools", None)
        response = await self._client.request("thread/resume", forwarded, timeout=timeout)
        self._confirm_profile(response, combined)
        if response.get("thread", {}).get("id") != record["nativeId"]:
            raise AppServerError("native Codex resumed an unexpected thread")
        record["params"] = combined
        record["closed"] = False
        self._loaded.add(record["id"])
        self._save(record)
        return self._rewrite(response, record["nativeId"])

    async def _turn_start(self, record: dict[str, Any], params: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self._lock(record["id"]):
            host_turn = _identifier(params.get("turnId"), "turnId")
            if record.get("closed") or record.get("activeTurnId") or host_turn in record["turnIds"].values():
                raise AppServerError("native Codex turn is closed, active or already dispatched")
            if record["id"] not in self._loaded:
                await self._resume(record, {}, timeout)
            await self._subscription(timeout)
            model = _model(params.get("model", record["model"]))
            if model != record["model"]:
                raise AppServerError("native Codex model changes require a new thread")
            await self._validate({**params, "model": model}, timeout)
            forwarded = {key: deepcopy(params[key]) for key in _TURN_FIELDS if key in params}
            if record["params"].get("sandbox", "workspace-write") != "danger-full-access":
                # Preserve the thread's scoped filesystem/network permission profile.
                forwarded.pop("sandboxPolicy", None)
            if _effort(params) is not None:
                forwarded["effort"] = _effort(params)
            if isinstance(forwarded.get("input"), str):
                forwarded["input"] = [{"type": "text", "text": forwarded["input"], "text_elements": []}]
            forwarded.update(threadId=record["nativeId"], model=model, serviceTier=_tier(params.get("serviceTier")))
            record["activeTurnId"] = host_turn
            self._pending_turns[record["nativeId"]] = host_turn
            self._save(record)
            try:
                response = await self._client.request("turn/start", forwarded, timeout=timeout)
                native_turn = _identifier(response.get("turn", {}).get("id"), "native turnId")
                self._bind_turn(record, native_turn, host_turn)
                await self._flush_deferred()
                return self._rewrite(response, record["nativeId"])
            except BaseException:
                # Stopping the native process also fences native commands, not just Bello tools.
                await self._fail(AppServerError("native turn/start outcome uncertain; request was not replayed"))
                raise
            finally:
                self._pending_turns.pop(record["nativeId"], None)

    def _bind_turn(self, record: dict[str, Any], native: str, host: str) -> None:
        existing = record["turnIds"].get(native)
        reverse = next((n for n, h in record["turnIds"].items() if h == host), None)
        if existing not in (None, host) or reverse not in (None, native):
            raise AppServerError("native Codex turn identity changed before acknowledgement")
        record["turnIds"][native] = host
        self._native_turns[(record["nativeId"], native)] = host
        self._save(record)

    def _save(self, record: dict[str, Any]) -> None:
        self._journal.save_thread(record["id"], record)

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {"id": record["id"], "model": record["model"], "cwd": record["cwd"],
                "engine": "codex", "nativeThreadId": record["nativeId"], "turns": []}

    def reconcile_terminal_turn(self, thread_id: str, turn_id: str,
                                turn: dict[str, Any]) -> bool:
        """Fence only the active mapped turn after a controller-owned read probe."""
        if (self._closing or self._failure is not None or not isinstance(thread_id, str)
                or not isinstance(turn_id, str) or not turn_id or not isinstance(turn, dict)
                or turn.get("id") != turn_id
                or turn.get("status") not in ("completed", "failed", "interrupted")):
            return False
        record = self._threads.get(thread_id)
        if (record is None or record.get("closed") or record.get("activeTurnId") not in (None, turn_id)
                or turn_id not in record.get("turnIds", {}).values()):
            return False
        # The native completion event may have already cleared this exact mapped
        # turn while host cleanup awaited. A different active turn is never touched.
        record.pop("activeTurnId", None)
        self._save(record)
        return True

    def _to_native(self, record: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(params)
        result["threadId"] = record["nativeId"]
        reverse = {host: native for native, host in record["turnIds"].items()}
        for key in ("turnId", "expectedTurnId"):
            if key in result:
                if result[key] not in reverse:
                    raise AppServerError("unknown native Codex turn identity")
                result[key] = reverse[result[key]]
        return result

    def _rewrite(self, value: Any, native_thread: str | None = None, kind: str | None = None) -> Any:
        if isinstance(value, list):
            return [self._rewrite(v, native_thread, kind) for v in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        thread = value.get("threadId", native_thread)
        if kind == "thread":
            thread = value.get("id", thread)
        result = {}
        for key, item in value.items():
            if key in _IDENTITY_FIELDS or key == "id" and kind == "thread":
                result[key] = self._native_threads.get(item, item) if isinstance(item, str) else item
            elif key in {"turnId", "expectedTurnId"} or key == "id" and kind == "turn":
                result[key] = self._native_turns.get((thread, item), item) if isinstance(item, str) else item
            elif key == "requestId":
                result[key] = self._request_history.get(item, item)
            elif key in {"thread", "turn", "turns"}:
                result[key] = self._rewrite(item, thread, "turn" if key == "turns" else key)
            elif isinstance(item, (dict, list)):
                result[key] = self._rewrite(item, thread)
            else:
                result[key] = item
        return result

    async def _receive(self, message: AppServerMessage) -> None:
        # Never await a handler/RPC from AppServerClient's sole reader task.
        self._queue.put_nowait(deepcopy(message.raw))

    async def _events(self) -> None:
        try:
            while True:
                raw = await self._queue.get()
                try:
                    await self._event(raw)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._spawn(self._fail(exc))

    async def _event(self, raw: dict[str, Any]) -> None:
        params = raw.get("params", {})
        native_thread = params.get("threadId") or params.get("thread", {}).get("id")
        if native_thread and native_thread not in self._native_threads:
            if self._starting_thread:
                if len(self._deferred) >= 2048:
                    raise AppServerError("native Codex pre-ack event queue exceeded its bound")
                self._deferred.append(raw)
            return
        host = self._native_threads.get(native_thread)
        record = self._threads.get(host)
        native_turn = params.get("turnId") or params.get("turn", {}).get("id")
        if record and native_turn and (native_thread, native_turn) not in self._native_turns:
            pending = self._pending_turns.get(native_thread)
            if not pending:
                return  # Unowned/historical native turns cannot acquire host authority.
            self._bind_turn(record, native_turn, pending)
        mapped = self._rewrite(raw, native_thread)
        if "id" in raw:
            self._spawn(self._server_request(raw, mapped, record))
            return
        if record and raw.get("method") == "turn/completed":
            completed = self._native_turns.get((native_thread, native_turn))
            if record.get("activeTurnId") == completed:
                record.pop("activeTurnId", None)
                self._save(record)
        if host:
            mapped.setdefault("params", {}).setdefault("threadId", host)
        await self.emit(mapped)
        if raw.get("method") == "serverRequest/resolved":
            self._request_history.pop(params.get("requestId"), None)

    async def _flush_deferred(self) -> None:
        deferred, self._deferred = self._deferred, []
        for raw in deferred:
            await self._event(raw)

    async def _server_request(self, raw: dict[str, Any], mapped: dict[str, Any], record: dict[str, Any] | None) -> None:
        if raw.get("method") == "item/tool/call":
            params = mapped.get("params", {})
            name = params.get("tool", "")
            allowed = {"bello_" + t["name"] for t in (record or {}).get("params", {}).get("tools", []) if t.get("name") in _DELEGATION}
            if not record or name not in allowed or not self.tool_handler:
                result = {"contentItems": [{"type": "inputText", "text": "Bello rejected an unconfigured delegation tool."}], "success": False}
            else:
                try:
                    result = await self.tool_handler({"threadId": record["id"], "turnId": params["turnId"],
                        "callId": "codex:" + record["id"] + ":" + str(params.get("callId", raw["id"])),
                        "name": name.removeprefix("bello_"), "arguments": params.get("arguments", {})})
                    result = {"contentItems": [{"type": "inputText", "text": c["text"]} for c in result.get("content", []) if c.get("type") == "text"],
                              "success": not result.get("isError", False)}
                except Exception:
                    result = {"contentItems": [{"type": "inputText", "text": "Bello delegation failed."}], "success": False}
            await self._client.respond(raw["id"], result)
            return
        request_id = "codex:" + str(uuid4())
        self._requests[request_id] = raw["id"]
        self._request_history[raw["id"]] = request_id
        while len(self._request_history) > 4096:
            self._request_history.popitem(last=False)
        mapped["id"] = request_id
        if record and record["params"].get("approvalPolicy") == "never" and raw.get("method") in _APPROVALS:
            # This should not be emitted by approvalPolicy=never. Do not grant an escalation.
            await self.respond(request_id, {"permissions": {}} if "permissions" in raw["method"] else {"decision": "decline"})
            return
        await self.emit(mapped)

    async def respond(self, request_id, result=None, *, error=None, timeout=15) -> bool:
        native = self._requests.get(request_id)
        if native is None:
            return False
        await self._client.respond(native, result, error=error, timeout=timeout)
        self._requests.pop(request_id, None)
        return True

    def _spawn(self, operation) -> None:
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None and self._failure is None and not self._closing:
                self._spawn(self._fail(done.exception()))
        task.add_done_callback(finished)

    async def _transport_error(self, error: BaseException) -> None:
        self._spawn(self._fail(error))

    async def _fail(self, error: BaseException) -> None:
        if self._failure is not None:
            return
        self._failure = error
        self._loaded.clear()
        if self._client:
            try:
                await asyncio.wait_for(self._client.stop(), 10)
            except Exception as cleanup_error:
                error.add_note(f"Native process cleanup also failed: {type(cleanup_error).__name__}")
        for record in self._threads.values():
            if record.pop("activeTurnId", None):
                self._save(record)
        if self.on_error:
            result = self.on_error(error)
            if inspect.isawaitable(result):
                await result

    async def stop(self) -> None:
        if self._closing:
            return
        self._closing = True
        current = asyncio.current_task()
        if self._pump and self._pump is not current:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        tasks = [t for t in self._tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self._client:
                await self._client.stop()
        finally:
            if self._bridge:
                await self._bridge.close()
            self._requests.clear()
            self._request_history.clear()
            self._loaded.clear()
            self._journal.close()
