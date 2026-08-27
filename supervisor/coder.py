from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supervisor.appserver import (
    APP_SERVER_CODER_RPC_TIMEOUT_SECONDS,
    APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    AppServerClient,
    AppServerError,
    text_input,
)
from supervisor.prompts import build_coder_prompt, build_restart_prompt
from supervisor.project_config import MultiAgentConfig
from supervisor.state import StateStore


CODER_SANDBOX_ENV = "BELLO_CODER_SANDBOX"
CODER_SANDBOX_READ_ONLY = "read-only"
CODER_SANDBOX_WORKSPACE_WRITE = "workspace-write"
CODER_SANDBOX_DANGER_FULL_ACCESS = "danger-full-access"
CODEX_FAST_SERVICE_TIER = "priority"
DEFAULT_INTELLIGENCE = "xhigh"


def coder_sandbox_mode() -> str:
    raw = os.environ.get(CODER_SANDBOX_ENV, CODER_SANDBOX_WORKSPACE_WRITE).strip().lower()
    aliases = {
        "read-only": CODER_SANDBOX_READ_ONLY,
        "readonly": CODER_SANDBOX_READ_ONLY,
        "read_only": CODER_SANDBOX_READ_ONLY,
        "workspace-write": CODER_SANDBOX_WORKSPACE_WRITE,
        "workspace_write": CODER_SANDBOX_WORKSPACE_WRITE,
        "workspacewrite": CODER_SANDBOX_WORKSPACE_WRITE,
        "danger-full-access": CODER_SANDBOX_DANGER_FULL_ACCESS,
        "danger_full_access": CODER_SANDBOX_DANGER_FULL_ACCESS,
        "danger": CODER_SANDBOX_DANGER_FULL_ACCESS,
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        supported = f"{CODER_SANDBOX_READ_ONLY}, {CODER_SANDBOX_WORKSPACE_WRITE}, {CODER_SANDBOX_DANGER_FULL_ACCESS}"
        raise RuntimeError(f"unsupported {CODER_SANDBOX_ENV}={raw!r}; expected one of: {supported}") from exc


def coder_turn_sandbox_policy(project_root: Path | None = None) -> dict[str, Any]:
    mode = coder_sandbox_mode()
    if mode == CODER_SANDBOX_DANGER_FULL_ACCESS:
        return {"type": "dangerFullAccess"}
    if mode == CODER_SANDBOX_WORKSPACE_WRITE:
        if project_root is None:
            raise RuntimeError("workspace-write coder sandbox requires a project root")
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(project_root.resolve())],
            "networkAccess": False,
        }
    return {"type": "readOnly", "networkAccess": False}


def codex_service_tier(*, fast: bool) -> str | None:
    return CODEX_FAST_SERVICE_TIER if fast else None


def apply_intelligence(params: dict[str, Any], intelligence: str | None) -> dict[str, Any]:
    if intelligence:
        params["effort"] = intelligence
    return params


def build_multi_agent_developer_instructions(config: MultiAgentConfig) -> str | None:
    if not config.enabled:
        return None
    allowed = "\n".join(
        f"- {model}: {', '.join(efforts)}"
        for model, efforts in config.allowed.items()
    )
    return (
        "Use subagents when independent delegation would materially improve speed or quality.\n"
        "Before spawning each subagent, choose the fastest and least expensive allowed profile that can "
        "reliably complete its task. Use a stronger profile for ambiguous, cross-cutting, or "
        "correctness-critical work.\n"
        "Choose only from these allowed model and reasoning-effort combinations:\n"
        f"{allowed}\n"
        f"The default subagent profile is {config.default.model} at {config.default.intelligence} effort.\n"
        "Use the default profile when there is no clear task-specific reason to choose another allowed profile.\n"
        "Wait for every subagent whose result affects task completion."
    )


def coder_thread_params(
    project_root: Path,
    *,
    model: str | None = None,
    fast: bool = False,
    multi_agent: MultiAgentConfig | None = None,
) -> dict[str, Any]:
    multi_agent = multi_agent or MultiAgentConfig()
    params: dict[str, Any] = {
        "cwd": str(project_root),
        "runtimeWorkspaceRoots": [str(project_root)],
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": coder_sandbox_mode(),
        "serviceTier": codex_service_tier(fast=fast),
        "ephemeral": False,
        "experimentalRawEvents": False,
        "persistExtendedHistory": False,
        "config": {
            "agents": {
                "enabled": multi_agent.enabled,
            }
        },
    }
    if multi_agent.enabled:
        params["config"]["agents"].update(
            {
                "max_concurrent_threads_per_session": multi_agent.max_concurrent,
                "default_subagent_model": multi_agent.default.model,
                "default_subagent_reasoning_effort": multi_agent.default.intelligence,
            }
        )
        params["developerInstructions"] = build_multi_agent_developer_instructions(multi_agent)
    if model:
        params["model"] = model
    return params


def coder_turn_params(
    thread_id: str,
    text: str,
    project_root: Path,
    *,
    model: str | None = None,
    fast: bool = False,
    intelligence: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [text_input(text)],
        "cwd": str(project_root),
        "runtimeWorkspaceRoots": [str(project_root)],
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": coder_turn_sandbox_policy(project_root),
        "serviceTier": codex_service_tier(fast=fast),
    }
    if model:
        params["model"] = model
    return apply_intelligence(params, intelligence)


@dataclass
class CoderSession:
    client: AppServerClient
    store: StateStore
    project_root: Path
    task_path: Path
    model: str | None = None
    fast: bool = False
    intelligence: str | None = DEFAULT_INTELLIGENCE
    thread_id: str | None = None
    active_turn_id: str | None = None
    coder_rpc_timeout_seconds: float = APP_SERVER_CODER_RPC_TIMEOUT_SECONDS
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)

    async def start_thread(self) -> str:
        response = await self.client.thread_start(
            coder_thread_params(
                self.project_root,
                model=self.model,
                fast=self.fast,
                multi_agent=self.multi_agent,
            ),
            timeout=APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
        )
        thread = response.get("thread", {})
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError("app-server thread/start did not return a thread id")
        self.thread_id = thread_id
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"coder_thread_id": thread_id}))
        return thread_id

    async def start_initial_turn(self) -> str:
        return await self.start_turn(build_coder_prompt(self.task_path))

    async def start_restart_turn(self) -> str:
        return await self.start_turn(build_restart_prompt(self.task_path))

    async def start_turn(self, message: str) -> str:
        thread_id = self.thread_id or await self.start_thread()
        response = await self.client.turn_start(
            coder_turn_params(
                thread_id,
                message,
                self.project_root,
                model=self.model,
                fast=self.fast,
                intelligence=self.intelligence,
            ),
            timeout=self.coder_rpc_timeout_seconds,
        )
        turn = response.get("turn", {})
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise RuntimeError("app-server turn/start did not return a turn id")
        self.active_turn_id = turn_id
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": turn_id}))
        return turn_id

    async def steer_or_start(self, message: str) -> str | None:
        if self.thread_id and self.active_turn_id:
            try:
                await self.client.turn_steer(
                    self.thread_id,
                    self.active_turn_id,
                    message,
                    timeout=self.coder_rpc_timeout_seconds,
                )
                return self.active_turn_id
            except AppServerError:
                raise
            except Exception:
                self.active_turn_id = None
                self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
        if self.thread_id:
            return await self.start_turn(message)
        return None

    async def interrupt(self) -> None:
        if not self.thread_id or not self.active_turn_id:
            return
        await self.client.turn_interrupt(
            self.thread_id,
            self.active_turn_id,
            timeout=self.coder_rpc_timeout_seconds,
        )

    def mark_turn_completed(self, turn_id: str) -> None:
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
