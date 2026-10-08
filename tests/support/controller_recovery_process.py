"""Real controller process lifecycle with a deterministic, model-free provider.

The first worker exits abruptly at a coder checkpoint. The second must resume
the exact thread/snapshot and finish. No production recovery checks are patched.
Tests supplying their own fence receipt label that receipt as synthetic; native
watchdog tests can run this same worker with the real guardian.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from supervisor.controller import BelloController
from supervisor.controller_recovery import recovery_root
from supervisor.process_fence import configure_worker
from supervisor.schemas import AppEventSource, BelloStatus, ValidationRun
from tests.support.controller import _FakeTUI, _async_noop


class Provider:
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.controller = None
        self.resumed = False

    def note(self, kind: str, **fields) -> None:
        with self.evidence.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **fields}) + "\n")

    async def start(self):
        self.note("provider_started")

    async def initialize(self):
        return {}

    async def stop(self):
        self.note("provider_stopped")

    async def thread_start(self, params, **kwargs):
        self.note("initial_thread", cwd=params["cwd"])
        return {"thread": {"id": "persisted-coder-thread"}}

    async def thread_resume(self, params, **kwargs):
        assert params["threadId"] == "persisted-coder-thread"
        self.resumed = True
        self.note("thread_resumed", cwd=params["cwd"], sandbox=params["sandbox"],
                  approvals=params["approvalPolicy"])
        return {"thread": {"id": "persisted-coder-thread", "turns": [
            {"id": "initial-turn", "status": "interrupted", "items": []}]}}

    async def turn_start(self, params, **kwargs):
        self.note("continuation_turn" if self.resumed else "initial_turn")
        if self.resumed:
            (self.controller._active_workspace_root() / "solution.py").write_text("VALUE = 43\n", encoding="utf-8")
        return {"turn": {"id": "continuation-turn" if self.resumed else "initial-turn", "status": "inProgress"}}

    async def turn_interrupt(self, *args, **kwargs):
        return {}


async def run(project: Path, evidence: Path) -> None:
    configure_worker()
    provider = Provider(evidence)
    controller = BelloController(project, task_path=project / "TASK.md", client=provider,
        tui=_FakeTUI(), runtime_enabled=False, completion_review=False, adversary_enabled=False,
        use_git_diff=False)
    provider.controller = controller
    controller.preflight = _async_noop

    async def process_body():
        if not provider.resumed:
            snapshot = controller._coder_snapshot
            (snapshot.snapshot_root / "solution.py").write_text("VALUE = 42\n", encoding="utf-8")
            controller.store.update_bello_config(lambda cfg: cfg.model_copy(update={
                "generation": 2, "restart_count": 2, "adversary_run_count": 1,
                "completion_return_count": 1, "completion_returns_since_adversary": 1}))
            controller.store.patch_health(lambda health: health.model_copy(update={
                "generation": 2, "restart_count": 2, "denied_requests": 3}))
            controller._append_event(AppEventSource.SYSTEM, "fixture/checkpoint-evidence")
            controller.completion_attempt_count = 3
            controller.completion_restarts = 1
            controller.no_marker_idle_nudge_count = 1
            controller.provider_failure_recovery_counts = {"no_message": 1}
            controller.validations = [ValidationRun(command="fixture validation", sequence=controller._sequence,
                                                     passed=True, exit_code=0, summary="fixture passed")]
            controller.store.append_text_locked("DECISIONS.md", "- Preserve the selected implementation.\n")
            controller._write_run_checkpoint("coder", state="active")
            record = json.loads((recovery_root(project) / "run.json").read_text())
            assert record["eligible"], record["reason"]
            provider.note("crash_checkpoint", run_id=record["run_id"], snapshot=str(snapshot.snapshot_root),
                          generation=2, restarts=2, sequence=controller._sequence)
            os._exit(77)
        cfg = controller.store.get_bello_config()
        assert cfg.generation == cfg.restart_count == 2
        assert cfg.adversary_run_count == cfg.completion_return_count == 1
        assert controller.completion_attempt_count == 3
        assert controller.completion_restarts == controller.no_marker_idle_nudge_count == 1
        assert controller.provider_failure_recovery_counts == {"no_message": 1}
        assert controller.store.get_health().denied_requests == 3
        assert controller.validations[0].sequence == controller._sequence == 1
        assert "Preserve the selected" in controller.store.read_text("DECISIONS.md")
        provider.note("restored_evidence", run_id=controller._durable_run.run_id,
                      snapshot=str(controller._coder_snapshot.snapshot_root), generation=cfg.generation,
                      restarts=cfg.restart_count, sequence=controller._sequence)
        controller.coder.mark_turn_completed("continuation-turn")
        await controller.finalize("deterministic recovered controller completed", status=BelloStatus.COMPLETE)
        provider.note("completed", status=controller.store.get_bello_config().status.value)

    controller.event_loop = process_body
    await controller.run()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
