"""Children service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class ChildrenState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _coder_quiesce_mutex: compat.asyncio.Lock | None
    _deferred_completion_check: compat.QueuedSupervisorCheck | None
    _quiescing_coder_tree: bool
    _reviewer_thread_ids: compat.OrderedDict[str, None]
    _reviewer_thread_roles: dict[str, str]
    _subagent_policy_notified: set[str]
    _subagents: dict[str, compat.SubagentRuntimeState]


class ChildrenPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_active_coder_subagents',
        '_active_workspace_root',
        '_adversary_multi_agent_config',
        '_append_cleanup_error',
        '_append_event',
        '_cleanup_reviewer_descendants',
        '_completion_multi_agent_config',
        '_deliver_coder_message',
        '_enforce_subagent_profile',
        '_finalize_completion_review_disabled',
        '_interrupt_subagent',
        '_is_coder_descendant',
        '_multi_agent_config',
        '_readiness_reviewer_thread_limit',
        '_refresh_coder_subagents',
        '_refresh_reviewer_subagents',
        '_reviewer_descendant_depth',
        '_reviewer_role_for_thread',
        '_schedule_supervisor_check',
        '_sequence',
        '_subagent_depth',
        '_subagent_depth_from_root',
        '_subagent_multi_agent_policy',
        '_subagent_policy_notifications',
        '_subagent_registry',
        '_track_collab_agent_tool_call',
        '_upsert_subagent_thread',
        'client',
        'coder',
        'project_config',
        'store',
        'tui',
    })
    writes = frozenset({
    })


class Children:
    """Own children behavior; borrow only the declared port."""

    def __init__(self, ports: ChildrenPort) -> None:
        self.state = ChildrenState()
        self.ports = ports

    def _subagent_registry(self) -> dict[str, compat.SubagentRuntimeState]:
        registry = getattr(self.state, "_subagents", None)
        if registry is None:
            registry = {}
            self.state._subagents = registry
        return registry

    def _subagent_policy_notifications(self) -> set[str]:
        notified = getattr(self.state, "_subagent_policy_notified", None)
        if notified is None:
            notified = set()
            self.state._subagent_policy_notified = notified
        return notified

    async def _track_subagent_notification(
        self,
        method: str,
        params: dict[str, compat.Any],
        *,
        thread_id: str | None,
        turn_id: str | None,
        enforce_policy: bool = True,
    ) -> None:
        cfg = self.ports.store.get_bello_config()
        if method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, dict):
                self.ports._upsert_subagent_thread(thread, generation=cfg.generation)
        elif method == "thread/status/changed" and isinstance(thread_id, str):
            state = self.ports._subagent_registry().get(thread_id)
            if state is not None:
                state.status = compat._thread_status_type(params.get("status"))
                if state.status != "active":
                    state.active_turn_id = None
                state.last_sequence = self.ports._sequence
        elif method == "thread/closed" and isinstance(thread_id, str):
            state = self.ports._subagent_registry().get(thread_id)
            if state is not None:
                state.status = "shutdown"
                state.active_turn_id = None
                state.last_sequence = self.ports._sequence
        elif method == "turn/started" and isinstance(thread_id, str):
            state = self.ports._subagent_registry().get(thread_id)
            if state is not None:
                state.status = "active"
                state.active_turn_id = turn_id
                state.last_sequence = self.ports._sequence
        elif method == "turn/completed" and isinstance(thread_id, str):
            state = self.ports._subagent_registry().get(thread_id)
            if state is not None:
                state.status = compat._turn_terminal_status(params.get("turn"))
                state.active_turn_id = None
                state.last_sequence = self.ports._sequence

        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "collabAgentToolCall":
                await self.ports._track_collab_agent_tool_call(item, event_thread_id=thread_id)

        # A nested child can arrive before its parent notification. Re-evaluate ancestry
        # whenever the registry changes so the relevant role policy is enforced as soon as
        # the chain to a coder or reviewer root becomes known.
        if enforce_policy:
            for state in tuple(self.ports._subagent_registry().values()):
                if self.ports._subagent_multi_agent_policy(state.thread_id, cfg=cfg) is not None:
                    await self.ports._enforce_subagent_profile(state)

    def _upsert_subagent_thread(
        self,
        thread: dict[str, compat.Any],
        *,
        generation: int,
    ) -> compat.SubagentRuntimeState | None:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            return None
        parent_thread_id = thread.get("parentThreadId")
        registry = self.ports._subagent_registry()
        state = registry.get(thread_id)
        if state is None:
            state = compat.SubagentRuntimeState(
                thread_id=thread_id,
                parent_thread_id=parent_thread_id if isinstance(parent_thread_id, str) else None,
                generation=generation,
            )
            registry[thread_id] = state
        elif isinstance(parent_thread_id, str):
            state.parent_thread_id = parent_thread_id
        state.status = compat._thread_status_type(thread.get("status"))
        if state.status != "active":
            state.active_turn_id = None
        state.nickname = compat._optional_bounded_text(thread.get("agentNickname"), 120) or state.nickname
        state.role = compat._optional_bounded_text(thread.get("agentRole"), 120) or state.role
        if isinstance(thread.get("model"), str):
            state.model = thread["model"]
        if isinstance(thread.get("reasoningEffort"), str):
            state.reasoning_effort = thread["reasoningEffort"]
        state.last_sequence = max(state.last_sequence, self.ports._sequence)
        return state

    async def _track_collab_agent_tool_call(
        self,
        item: dict[str, compat.Any],
        *,
        event_thread_id: str | None,
    ) -> None:
        sender = item.get("senderThreadId")
        parent_thread_id = sender if isinstance(sender, str) else event_thread_id
        receivers = [value for value in item.get("receiverThreadIds") or [] if isinstance(value, str)]
        agents_states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
        cfg = self.ports.store.get_bello_config()
        for receiver in receivers:
            registry = self.ports._subagent_registry()
            state = registry.get(receiver)
            if state is None:
                state = compat.SubagentRuntimeState(
                    thread_id=receiver,
                    parent_thread_id=parent_thread_id,
                    generation=cfg.generation,
                )
                registry[receiver] = state
            elif state.parent_thread_id is None and isinstance(parent_thread_id, str):
                state.parent_thread_id = parent_thread_id
            if item.get("tool") == "spawnAgent":
                if isinstance(item.get("model"), str):
                    state.model = item["model"]
                if isinstance(item.get("reasoningEffort"), str):
                    state.reasoning_effort = item["reasoningEffort"]
                if isinstance(item.get("prompt"), str):
                    state.prompt = compat._bounded_subagent_text(item["prompt"], limit=600)
            agent_state = agents_states.get(receiver)
            if isinstance(agent_state, dict):
                state.status = str(agent_state.get("status") or state.status)
                message = agent_state.get("message")
                if isinstance(message, str) and message.strip():
                    state.last_message = compat._bounded_subagent_text(message, limit=600)
            elif isinstance(agent_state, str):
                state.status = agent_state
            if state.status in {"interrupted", "completed", "errored", "shutdown", "notFound"}:
                state.active_turn_id = None
            state.last_sequence = self.ports._sequence

    def _is_coder_descendant(
        self,
        thread_id: compat.Any,
        *,
        cfg: compat.BelloConfig | None = None,
    ) -> bool:
        if not isinstance(thread_id, str):
            return False
        cfg = cfg or self.ports.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        if not isinstance(root_thread_id, str) or thread_id == root_thread_id:
            return False
        seen: set[str] = set()
        current = thread_id
        for _ in range(32):
            if current in seen:
                return False
            seen.add(current)
            state = self.ports._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return False
            if state.parent_thread_id == root_thread_id:
                return state.generation == cfg.generation
            current = state.parent_thread_id
        return False

    def _subagent_depth_from_root(self, thread_id: compat.Any, root_thread_id: compat.Any) -> int | None:
        if not isinstance(thread_id, str) or not isinstance(root_thread_id, str):
            return None
        if thread_id == root_thread_id:
            return 0
        seen: set[str] = set()
        current = thread_id
        depth = 0
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self.ports._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            depth += 1
            if state.parent_thread_id == root_thread_id:
                return depth
            current = state.parent_thread_id
        return None

    def _subagent_depth(self, thread_id: str, *, cfg: compat.BelloConfig | None = None) -> int:
        cfg = cfg or self.ports.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        depth = 0
        current = thread_id
        seen: set[str] = set()
        while current not in seen and depth < 32:
            seen.add(current)
            state = self.ports._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                break
            depth += 1
            if state.parent_thread_id == root_thread_id:
                return max(1, depth)
            current = state.parent_thread_id
        return max(1, depth)

    def _active_coder_subagents(self) -> list[compat.SubagentRuntimeState]:
        cfg = self.ports.store.get_bello_config()
        active: list[compat.SubagentRuntimeState] = []
        for state in self.ports._subagent_registry().values():
            if not self.ports._is_coder_descendant(state.thread_id, cfg=cfg):
                continue
            if state.active_turn_id or state.status in {"active", "running", "pendingInit", "inProgress"}:
                active.append(state)
        return active

    def _subagent_summaries(self) -> list[compat.SubagentSummary]:
        cfg = self.ports.store.get_bello_config()
        states = [
            state
            for state in self.ports._subagent_registry().values()
            if self.ports._is_coder_descendant(state.thread_id, cfg=cfg)
        ]
        states.sort(
            key=lambda state: (
                not bool(state.active_turn_id or state.status in {"active", "running", "pendingInit", "inProgress"}),
                -state.last_sequence,
            )
        )
        summaries: list[compat.SubagentSummary] = []
        for state in states[:compat.SUBAGENT_SUMMARY_LIMIT]:
            parent = state.parent_thread_id
            if not isinstance(parent, str):
                continue
            actions = [
                compat.SubagentActivity(
                    sequence=sequence,
                    kind=kind,
                    summary=compat._bounded_subagent_text(summary, limit=400),
                    item_id=item_id,
                )
                for sequence, kind, summary, item_id in state.recent_actions[-compat.SUBAGENT_ACTION_LIMIT:]
            ]
            summaries.append(
                compat.SubagentSummary(
                    thread_id=state.thread_id,
                    parent_thread_id=parent,
                    depth=self.ports._subagent_depth(state.thread_id, cfg=cfg),
                    status=state.status,
                    active_turn_id=state.active_turn_id,
                    model=state.model,
                    reasoning_effort=state.reasoning_effort,
                    nickname=state.nickname,
                    role=state.role,
                    prompt=compat._optional_bounded_text(state.prompt, 600),
                    last_message=compat._optional_bounded_text(state.last_message, 600),
                    recent_actions=actions,
                    validation_ids=state.validation_ids[-8:],
                    last_event_sequence=state.last_sequence or None,
                    profile_allowed=state.profile_allowed,
                )
            )
        return summaries

    async def _enforce_subagent_profile(self, state: compat.SubagentRuntimeState) -> None:
        policy = self.ports._subagent_multi_agent_policy(state.thread_id)
        if policy is None:
            return
        role, multi_agent = policy
        default = getattr(multi_agent, "default", None)
        model = state.model or getattr(default, "model", None)
        intelligence = state.reasoning_effort or getattr(default, "intelligence", None)
        if not isinstance(model, str) or not isinstance(intelligence, str):
            return
        state.model = model
        state.reasoning_effort = intelligence
        allowed = bool(
            getattr(multi_agent, "enabled", False)
            and getattr(multi_agent, "is_allowed", lambda *_: False)(model, intelligence)
        )
        reviewer_depth = self.ports._reviewer_descendant_depth(state.thread_id)
        depth_allowed = role == "coder" or reviewer_depth == 1
        allowed = allowed and depth_allowed
        state.profile_allowed = allowed
        if allowed:
            return
        if state.active_turn_id:
            await self.ports._interrupt_subagent(
                state,
                cleanup_kind=f"{role}_subagent_policy",
            )
        notified = self.ports._subagent_policy_notifications()
        if state.thread_id in notified:
            return
        notified.add(state.thread_id)
        allowed_text = compat._format_allowed_subagent_profiles(multi_agent)
        if not depth_allowed:
            reason = (
                f"Reviewer subagent {state.thread_id} attempted nested delegation at depth "
                f"{reviewer_depth}; reviewer delegation is limited to one child level."
            )
        else:
            reason = (
                f"{role.replace('_', ' ').title()} subagent {state.thread_id} used forbidden profile "
                f"{model}/{intelligence}. Allowed profiles: {allowed_text}. Stop that child and, if "
                "delegation is still useful, spawn a replacement using an allowed profile."
            )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "subagent/depth_denied" if not depth_allowed else "subagent/profile_denied",
            thread_id=state.thread_id,
            reason=reason,
        )
        self.ports.store.append_text_locked(compat.PROGRESS, f"- {reason}\n")
        self.ports.tui.render("SUPERVISOR", reason)
        if role == "coder" and self.ports.coder is not None:
            await self.ports._deliver_coder_message(reason)

    def _subagent_multi_agent_policy(
        self,
        thread_id: compat.Any,
        *,
        cfg: compat.BelloConfig | None = None,
    ) -> tuple[str, compat.Any] | None:
        if self.ports._is_coder_descendant(thread_id, cfg=cfg):
            return "coder", self.ports._multi_agent_config()
        reviewer_role = self.ports._reviewer_role_for_thread(thread_id)
        reviewer_depth = self.ports._reviewer_descendant_depth(thread_id)
        if reviewer_depth is None or reviewer_depth < 1:
            return None
        if reviewer_role == "completion_review":
            return reviewer_role, self.ports._completion_multi_agent_config()
        if reviewer_role == "adversary":
            return reviewer_role, self.ports._adversary_multi_agent_config()
        return None

    def _multi_agent_config(self) -> compat.Any:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is None:
            return compat.MultiAgentConfig()
        return getattr(project_config, "multi_agent", None)

    def _completion_multi_agent_config(self) -> compat.Any:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is None:
            return compat.MultiAgentConfig()
        return getattr(project_config, "completion_multi_agent", compat.MultiAgentConfig())

    def _adversary_multi_agent_config(self) -> compat.Any:
        project_config = getattr(self.ports, "project_config", None)
        if project_config is None:
            return compat.MultiAgentConfig()
        return getattr(project_config, "adversary_multi_agent", compat.MultiAgentConfig())

    async def _refresh_coder_subagents(self) -> None:
        cfg = self.ports.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        client = getattr(self.ports, "client", None)
        if not isinstance(root_thread_id, str) or client is None or not hasattr(client, "thread_list"):
            return
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, compat.Any] = {
                "archived": False,
                "cwd": str(self.ports._active_workspace_root()),
                "limit": 100,
                "sourceKinds": list(compat.SUBAGENT_SOURCE_KINDS),
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.thread_list(params)
            threads = response.get("data")
            if not isinstance(threads, list):
                break
            for thread in threads:
                if isinstance(thread, dict):
                    self.ports._upsert_subagent_thread(thread, generation=cfg.generation)
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        for state in tuple(self.ports._subagent_registry().values()):
            if not self.ports._is_coder_descendant(state.thread_id, cfg=cfg):
                continue
            if state.status != "active" or state.active_turn_id is not None:
                continue
            if not hasattr(client, "thread_turns_list"):
                continue
            response = await client.thread_turns_list(
                state.thread_id,
                limit=1,
                items_view="summary",
                sort_direction="desc",
            )
            turns = response.get("data")
            if isinstance(turns, list) and turns:
                turn = turns[0]
                if isinstance(turn, dict) and turn.get("status") == "inProgress" and isinstance(turn.get("id"), str):
                    state.active_turn_id = turn["id"]

    async def _refresh_reviewer_subagents(
        self,
        root_thread_id: str,
        workspace_root: compat.Path,
    ) -> None:
        client = getattr(self.ports, "client", None)
        if client is None or not hasattr(client, "thread_list"):
            return
        cfg = self.ports.store.get_bello_config()
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, compat.Any] = {
                "archived": False,
                "cwd": str(workspace_root.resolve()),
                "limit": 100,
                "sourceKinds": list(compat.SUBAGENT_SOURCE_KINDS),
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.thread_list(params)
            threads = response.get("data")
            if not isinstance(threads, list):
                break
            for thread in threads:
                if isinstance(thread, dict):
                    self.ports._upsert_subagent_thread(thread, generation=cfg.generation)
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        if not hasattr(client, "thread_turns_list"):
            return
        for state in tuple(self.ports._subagent_registry().values()):
            if self.ports._subagent_depth_from_root(state.thread_id, root_thread_id) is None:
                continue
            if state.status != "active" or state.active_turn_id is not None:
                continue
            response = await client.thread_turns_list(
                state.thread_id,
                limit=1,
                items_view="summary",
                sort_direction="desc",
            )
            turns = response.get("data")
            if isinstance(turns, list) and turns:
                turn = turns[0]
                if (
                    isinstance(turn, dict)
                    and turn.get("status") == "inProgress"
                    and isinstance(turn.get("id"), str)
                ):
                    state.active_turn_id = turn["id"]

    async def _cleanup_completion_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: compat.Path,
    ) -> None:
        await self.ports._cleanup_reviewer_descendants(
            root_thread_id,
            workspace_root,
            cleanup_kind="completion_review_subagent",
        )

    async def _cleanup_adversary_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: compat.Path,
    ) -> None:
        await self.ports._cleanup_reviewer_descendants(
            root_thread_id,
            workspace_root,
            cleanup_kind="adversary_subagent",
        )

    async def _cleanup_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: compat.Path,
        *,
        cleanup_kind: str,
    ) -> None:
        try:
            await self.ports._refresh_reviewer_subagents(root_thread_id, workspace_root)
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind=f"{cleanup_kind}_refresh",
                thread_id=root_thread_id,
                turn_id=None,
                error=exc,
            )
        descendants = [
            (depth, state)
            for state in self.ports._subagent_registry().values()
            if (depth := self.ports._subagent_depth_from_root(state.thread_id, root_thread_id))
            is not None
            and depth > 0
        ]
        descendants.sort(key=lambda item: item[0], reverse=True)
        for _, state in descendants:
            if state.active_turn_id:
                try:
                    await self.ports._interrupt_subagent(
                        state,
                        cleanup_kind=f"{cleanup_kind}_interrupt",
                    )
                except Exception:
                    pass
            try:
                if hasattr(self.ports.client, "thread_archive"):
                    await self.ports.client.thread_archive(state.thread_id)
                elif hasattr(self.ports.client, "thread_unsubscribe"):
                    await self.ports.client.thread_unsubscribe(state.thread_id)
            except Exception as exc:
                self.ports._append_cleanup_error(
                    cleanup_kind=f"{cleanup_kind}_archive",
                    thread_id=state.thread_id,
                    turn_id=state.active_turn_id,
                    error=exc,
                )
                try:
                    if hasattr(self.ports.client, "thread_unsubscribe"):
                        await self.ports.client.thread_unsubscribe(state.thread_id)
                except Exception as unsubscribe_exc:
                    self.ports._append_cleanup_error(
                        cleanup_kind=f"{cleanup_kind}_unsubscribe",
                        thread_id=state.thread_id,
                        turn_id=state.active_turn_id,
                        error=unsubscribe_exc,
                    )
            state.status = "shutdown"
            state.active_turn_id = None

    async def _interrupt_subagent(
        self,
        state: compat.SubagentRuntimeState,
        *,
        cleanup_kind: str,
    ) -> None:
        if not state.active_turn_id:
            return
        try:
            await self.ports.client.turn_interrupt(state.thread_id, state.active_turn_id)
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind=cleanup_kind,
                thread_id=state.thread_id,
                turn_id=state.active_turn_id,
                error=exc,
            )
            raise
        state.status = "interrupted"
        state.active_turn_id = None

    async def _quiesce_coder_tree(self, reason: str, *, strict: bool = True) -> bool:
        mutex = getattr(self.state, "_coder_quiesce_mutex", None)
        if mutex is None:
            mutex = compat.asyncio.Lock()
            self.state._coder_quiesce_mutex = mutex
        async with mutex:
            self.state._quiescing_coder_tree = True
            try:
                coder = getattr(self.ports, "coder", None)
                if coder is not None:
                    try:
                        await coder.interrupt()
                    except Exception as exc:
                        self.ports._append_cleanup_error(
                            cleanup_kind=f"{reason}_coder_interrupt",
                            thread_id=getattr(coder, "thread_id", None) or "unknown",
                            turn_id=getattr(coder, "active_turn_id", None),
                            error=exc,
                        )
                        if strict:
                            raise
                        return False
                try:
                    for _ in range(3):
                        await self.ports._refresh_coder_subagents()
                        active = sorted(
                            self.ports._active_coder_subagents(),
                            key=lambda state: self.ports._subagent_depth(state.thread_id),
                            reverse=True,
                        )
                        if not active:
                            break
                        for state in active:
                            await self.ports._interrupt_subagent(
                                state,
                                cleanup_kind=f"{reason}_subagent_interrupt",
                            )
                    await self.ports._refresh_coder_subagents()
                    remaining = self.ports._active_coder_subagents()
                    if remaining:
                        ids = ", ".join(state.thread_id for state in remaining)
                        raise RuntimeError(f"coder descendants did not quiesce: {ids}")
                except Exception:
                    if strict:
                        raise
                    return False
                return True
            finally:
                self.state._quiescing_coder_tree = False

    async def _resume_deferred_completion_if_quiescent(self) -> None:
        queued = getattr(self.state, "_deferred_completion_check", None)
        if queued is None or self.ports._active_coder_subagents():
            return
        cfg = self.ports.store.get_bello_config()
        if cfg.active_coder_turn_id:
            return
        self.state._deferred_completion_check = None
        if not queued.completion_review:
            await self.ports._finalize_completion_review_disabled()
            return
        self.ports._schedule_supervisor_check(
            queued.summary,
            triggering_item_id=queued.triggering_item_id,
            triggering_action=queued.triggering_action,
            human_message=queued.human_message,
            patch_summary=queued.patch_summary,
            completion_review=queued.completion_review,
        )

    def _register_reviewer_thread(self, thread_id: str, *, role: str = "reviewer") -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        registry = getattr(self.state, "_reviewer_thread_ids", None)
        if not isinstance(registry, compat.OrderedDict):
            registry = compat.OrderedDict()
            self.state._reviewer_thread_ids = registry
        roles = getattr(self.state, "_reviewer_thread_roles", None)
        if not isinstance(roles, dict):
            roles = {}
            self.state._reviewer_thread_roles = roles
        registry[thread_id] = None
        roles[thread_id] = role
        registry.move_to_end(thread_id)
        limit = max(
            1,
            int(
                getattr(
                    self.ports,
                    "_readiness_reviewer_thread_limit",
                    compat.READINESS_REVIEWER_THREAD_LIMIT,
                )
            ),
        )
        while len(registry) > limit:
            evicted_thread_id, _ = registry.popitem(last=False)
            roles.pop(evicted_thread_id, None)

    def _reviewer_role_for_thread(self, thread_id: compat.Any) -> str | None:
        if not isinstance(thread_id, str):
            return None
        roles = getattr(self.state, "_reviewer_thread_roles", {})
        if not isinstance(roles, dict):
            roles = {}
        registry = getattr(self.state, "_reviewer_thread_ids", {})
        reviewer_roots = registry if isinstance(registry, dict) else {}
        direct = roles.get(thread_id)
        if isinstance(direct, str):
            return direct
        if thread_id in reviewer_roots:
            return "reviewer"
        current = thread_id
        seen: set[str] = set()
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self.ports._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            parent = state.parent_thread_id
            role = roles.get(parent)
            if isinstance(role, str):
                return role
            if parent in reviewer_roots:
                return "reviewer"
            current = parent
        return None

    def _reviewer_descendant_depth(self, thread_id: compat.Any) -> int | None:
        if not isinstance(thread_id, str):
            return None
        roles = getattr(self.state, "_reviewer_thread_roles", {})
        if not isinstance(roles, dict):
            roles = {}
        registry = getattr(self.state, "_reviewer_thread_ids", {})
        reviewer_roots = registry if isinstance(registry, dict) else {}
        roots = set(roles) | set(reviewer_roots)
        if thread_id in roots:
            return 0
        if not roots:
            return None
        current = thread_id
        seen: set[str] = set()
        depth = 0
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self.ports._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            depth += 1
            parent = state.parent_thread_id
            if parent in roots:
                return depth
            current = parent
        return None
