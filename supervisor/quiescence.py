"""Non-mutating queue checks used by read-only execution-status probes."""
from collections import deque
from typing import Any


def _unthreaded_deprecation_notice(value: Any) -> bool:
    if not isinstance(value, dict):
        # Controller queues wrap provider notifications; all other controller
        # events (including approvals, transport errors and shutdown) block.
        if getattr(value, "kind", None) != "notification":
            return False
        value = getattr(getattr(value, "message", None), "raw", None)
    if (not isinstance(value, dict) or value.get("method") != "deprecationNotice"
            or set(value) - {"method", "params", "jsonrpc"}):
        return False
    params = value.get("params")
    return isinstance(params, dict) and not any(
        key in params for key in ("threadId", "thread", "turnId", "turn", "itemId", "item", "requestId")
    )


def execution_queue_clear(queue: Any) -> bool:
    """Ignore only identity-free deprecation notices, never consume events.

    asyncio.Queue has no public peek API. Its same-loop deque is inspected
    synchronously, with no await or mutation; unknown queue implementations and
    oversized inventories fail closed. All notices remain queued for auditing.
    """
    if queue is None:
        return True
    empty = getattr(queue, "empty", None)
    if not callable(empty):
        return False
    if empty():
        return True
    pending = getattr(queue, "_queue", None)
    if not isinstance(pending, deque) or len(pending) > 256:
        return False
    return all(_unthreaded_deprecation_notice(row) for row in tuple(pending))
