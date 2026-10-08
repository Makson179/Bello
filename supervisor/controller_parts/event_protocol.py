"""Controller event protocol; compatibility exports live in controller."""
from __future__ import annotations

from . import compat
from .defaults import SUBAGENT_TEXT_LIMIT


def _turn_id_from_params(params: dict[str, compat.Any]) -> str | None:
    if isinstance(params.get("turnId"), str):
        return params["turnId"]
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    return None


def _notification_thread_id(method: str, params: dict[str, compat.Any]) -> str | None:
    thread_id = params.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    if method == "thread/started":
        thread = params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    return None


def _thread_status_type(value: compat.Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"]
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _turn_terminal_status(value: compat.Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return value["status"]
    return "idle"


def _bounded_subagent_text(value: compat.Any, *, limit: int = SUBAGENT_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _optional_bounded_text(value: compat.Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return compat._bounded_subagent_text(value, limit=limit)


def _short_thread_id(thread_id: compat.Any) -> str:
    if not isinstance(thread_id, str):
        return "unknown"
    return thread_id if len(thread_id) <= 12 else thread_id[:12]


def _format_multi_agent_summary(multi_agent: compat.Any) -> str:
    if not getattr(multi_agent, "enabled", False):
        return "off"
    default = getattr(multi_agent, "default", None)
    return (
        f"on(max={getattr(multi_agent, 'max_concurrent', '?')},"
        f"default={getattr(default, 'model', '?')}/{getattr(default, 'intelligence', '?')})"
    )


def _format_allowed_subagent_profiles(multi_agent: compat.Any) -> str:
    allowed = getattr(multi_agent, "allowed", {})
    if not isinstance(allowed, dict) or not allowed:
        return "none"
    return "; ".join(
        f"{model}: {', '.join(str(effort) for effort in efforts)}"
        for model, efforts in allowed.items()
    )


def _bounded_subagent_event_payload(method: str, params: dict[str, compat.Any]) -> dict[str, compat.Any]:
    if method == "thread/started":
        thread = params.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("parentThreadId"), str):
            return {}
        return {
            "parent_thread_id": thread["parentThreadId"],
            "status": compat._thread_status_type(thread.get("status")),
            "nickname": compat._optional_bounded_text(thread.get("agentNickname"), 120),
            "role": compat._optional_bounded_text(thread.get("agentRole"), 120),
        }
    if method == "thread/status/changed":
        return {"status": compat._thread_status_type(params.get("status"))}
    if method not in {"item/started", "item/completed"}:
        return {}
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") != "collabAgentToolCall":
        return {}
    agents_states = item.get("agentsStates")
    bounded_states: dict[str, compat.Any] = {}
    if isinstance(agents_states, dict):
        for thread_id, state in list(agents_states.items())[:compat.SUBAGENT_SUMMARY_LIMIT]:
            if not isinstance(thread_id, str):
                continue
            bounded_states[thread_id] = (
                state.get("status") if isinstance(state, dict) else state
            )
    return {
        "tool": item.get("tool"),
        "sender_thread_id": item.get("senderThreadId"),
        "receiver_thread_ids": [
            value for value in (item.get("receiverThreadIds") or [])[:compat.SUBAGENT_SUMMARY_LIMIT]
            if isinstance(value, str)
        ],
        "model": item.get("model"),
        "reasoning_effort": item.get("reasoningEffort"),
        "status": item.get("status"),
        "prompt": compat._optional_bounded_text(item.get("prompt"), 600),
        "agents_states": bounded_states,
    }


def _item_id_from_params(params: dict[str, compat.Any]) -> str | None:
    if isinstance(params.get("itemId"), str):
        return params["itemId"]
    item = params.get("item")
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return None


def _item_summary(item: compat.Any) -> str:
    if not isinstance(item, dict):
        return "item completed"
    item_type = item.get("type", "item")
    if item_type == "commandExecution":
        return f"command completed: {item.get('command', '')} exit={item.get('exitCode')}"
    if item_type == "fileChange":
        return f"file change completed: {len(item.get('changes') or [])} changes"
    if item_type == "fileRead":
        return f"file inspection completed: {item.get('tool')} {', '.join(item.get('paths') or [])}"
    if item_type == "mcpToolCall":
        return f"mcp tool completed: {item.get('server')}/{item.get('tool')}"
    if item_type == "dynamicToolCall":
        return f"dynamic tool completed: {item.get('tool')}"
    if item_type == "agentMessage":
        return "agent message completed"
    return f"{item_type} completed"


def _is_completed_action(item: compat.Any) -> bool:
    return isinstance(item, dict) and item.get("type") in {"commandExecution", "fileChange", "fileRead", "mcpToolCall", "dynamicToolCall", "webSearch"}


def _is_stream_delta_method(method: str) -> bool:
    lowered = method.lower()
    return lowered.endswith("delta") or method in {
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "command/exec/outputDelta",
        "process/outputDelta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
    }


def _is_command_output_delta_method(method: str) -> bool:
    lowered = method.lower()
    if method in {
        "item/commandExecution/outputDelta",
        "command/exec/outputDelta",
        "process/outputDelta",
    }:
        return True
    return any(token in lowered for token in ("command", "exec", "process")) and (
        lowered.endswith("outputdelta")
        or lowered.endswith("stdoutdelta")
        or lowered.endswith("stderrdelta")
    )
