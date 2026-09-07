from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from supervisor.appserver import (
    APP_SERVER_CODER_RPC_TIMEOUT_SECONDS,
    APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    AppServerClient,
    AppServerError,
    text_input,
)
from supervisor.prompts import build_coder_prompt, build_restart_prompt, build_revision_prompt
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


MultiAgentRole = Literal["coder", "completion_review", "adversary"]


def build_multi_agent_developer_instructions(
    config: MultiAgentConfig,
    *,
    role: MultiAgentRole = "coder",
) -> str | None:
    if not config.enabled:
        return None
    allowed = "\n".join(
        f"- {model}: {', '.join(efforts)}"
        for model, efforts in config.allowed.items()
    )
    shared = (
        "Use subagents when independent delegation would materially improve speed or quality.\n"
        "Before spawning each subagent, choose the fastest and least expensive allowed profile that can "
        "reliably complete its task. Use a stronger profile for ambiguous, cross-cutting, or "
        "correctness-critical work.\n"
        "Choose only from these allowed model and reasoning-effort combinations:\n"
        f"{allowed}\n"
        f"The default subagent profile is {config.default.model} at {config.default.intelligence} effort.\n"
        "Use the default profile when there is no clear task-specific reason to choose another allowed profile.\n"
    )
    if role == "coder":
        return shared + "Wait for every subagent whose result affects task completion."
    role_text = (
        "Delegate only bounded, independent investigations; do not delegate the final judgment or final output.\n"
        "Independently verify relevant subagent findings before relying on them in the final review.\n"
        "Keep delegation one level deep: do not ask subagents to spawn other subagents.\n"
        "Wait for every subagent whose result affects the final review."
    )
    if role == "completion_review":
        return (
            shared
            + "Use subagents to inspect distinct requirements, modules, or validation questions when useful.\n"
            + role_text
        )
    return (
        shared
        + "Use subagents to probe distinct attack surfaces, edge-case classes, or failure hypotheses when useful.\n"
        + role_text
    )


def apply_multi_agent_thread_start_params(
    params: dict[str, Any],
    config: MultiAgentConfig,
    *,
    role: MultiAgentRole = "coder",
) -> dict[str, Any]:
    agents: dict[str, Any] = {"enabled": config.enabled}
    if config.enabled:
        agents.update(
            {
                "max_concurrent_threads_per_session": config.max_concurrent,
                "default_subagent_model": config.default.model,
                "default_subagent_reasoning_effort": config.default.intelligence,
                "allowed_profiles": {model: list(efforts) for model, efforts in config.allowed.items()},
                "role": role,
            }
        )
        params["developerInstructions"] = build_multi_agent_developer_instructions(
            config,
            role=role,
        )
    raw_config = params.setdefault("config", {})
    if not isinstance(raw_config, dict):
        raise TypeError("thread/start config must be an object")
    raw_config["agents"] = agents
    return params


def coder_thread_params(
    project_root: Path,
    *,
    model: str | None = None,
    fast: bool = False,
    intelligence: str | None = None,
    multi_agent: MultiAgentConfig | None = None,
) -> dict[str, Any]:
    multi_agent = multi_agent or MultiAgentConfig()
    params: dict[str, Any] = {
        "cwd": str(project_root),
        "runtimeWorkspaceRoots": [str(project_root.resolve())],
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": coder_sandbox_mode(),
        "serviceTier": codex_service_tier(fast=fast),
        "ephemeral": False,
        "experimentalRawEvents": False,
        "persistExtendedHistory": False,
        "config": {},
    }
    apply_multi_agent_thread_start_params(params, multi_agent, role="coder")
    if model:
        params["model"] = model
    return apply_intelligence(params, intelligence)


def coder_thread_resume_params(
    thread_id: str,
    project_root: Path,
    *,
    model: str | None = None,
    fast: bool = False,
    intelligence: str | None = None,
    multi_agent: MultiAgentConfig | None = None,
) -> dict[str, Any]:
    """Build the supported ``thread/resume`` overrides for a coder thread."""

    multi_agent = multi_agent or MultiAgentConfig()
    params: dict[str, Any] = {
        "threadId": thread_id,
        "cwd": str(project_root.resolve()),
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": coder_sandbox_mode(),
        "serviceTier": codex_service_tier(fast=fast),
        "config": {},
    }
    apply_multi_agent_thread_start_params(params, multi_agent, role="coder")
    if model:
        params["model"] = model
    return apply_intelligence(params, intelligence)


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
        "runtimeWorkspaceRoots": [str(project_root.resolve())],
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
    plan_path: Path | None = None

    async def start_thread(self, *, persist_state: bool = True) -> str:
        response = await self.client.thread_start(
            coder_thread_params(
                self.project_root,
                model=self.model,
                fast=self.fast,
                intelligence=self.intelligence,
                multi_agent=self.multi_agent,
            ),
            timeout=APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
        )
        thread = response.get("thread", {})
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError("app-server thread/start did not return a thread id")
        self.thread_id = thread_id
        if persist_state:
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"coder_thread_id": thread_id}))
        return thread_id

    async def resume_thread(self) -> dict[str, Any]:
        if not self.thread_id:
            raise RuntimeError("cannot resume coder thread without a thread id")
        response = await self.client.thread_resume(
            coder_thread_resume_params(
                self.thread_id,
                self.project_root,
                model=self.model,
                fast=self.fast,
                intelligence=self.intelligence,
                multi_agent=self.multi_agent,
            ),
            timeout=APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
        )
        thread = response.get("thread", {})
        if not isinstance(thread, dict) or thread.get("id") != self.thread_id:
            raise RuntimeError("app-server thread/resume did not return the requested coder thread")
        return thread

    async def start_initial_turn(self) -> str:
        return await self.start_turn(
            build_coder_prompt(self.task_path, plan_path=self.plan_path)
        )

    async def start_restart_turn(self) -> str:
        return await self.start_turn(
            build_restart_prompt(self.task_path, plan_path=self.plan_path)
        )

    async def start_revision_turn(self, reviewer_feedback: str, *, persist_state: bool = True) -> str:
        return await self.start_turn(
            build_revision_prompt(self.task_path, reviewer_feedback),
            persist_state=persist_state,
        )

    async def start_turn(self, message: str, *, persist_state: bool = True) -> str:
        thread_id = self.thread_id or await self.start_thread(persist_state=persist_state)
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
        self.active_turn_id = None if turn.get("status") in {"completed", "failed", "interrupted"} else turn_id
        if persist_state:
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": self.active_turn_id}))
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
