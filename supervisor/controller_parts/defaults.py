"""Import-time defaults shared by the coordinator and standalone helpers.

Runtime lookups still use the public controller names so patches remain live.
"""

COMPLETION_EVIDENCE_FILE_LIMIT = 500
SUBAGENT_TEXT_LIMIT = 800
NO_MARKER_IDLE_NUDGE = (
    "Continue working. If you believe the task is ready, provide Summary, Validation evidence, "
    "and the exact readiness marker on its own line: BELLO_READY_FOR_REVIEW."
)
