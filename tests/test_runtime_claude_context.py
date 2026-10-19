from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock, UserMessage

from supervisor.appserver import AppServerError
from supervisor.runtime import claude_context
from tests.test_runtime_claude import FakeClient, FakeFactory, backend, result_message, wait_completed


class ContextClient(FakeClient):
    async def get_context_usage(self):
        return {"totalTokens": 100_000 if self.factory.main_queries >= 2 and self.options.resume else 100,
                "maxTokens": 200_000, "autoCompactThreshold": 180_000}

    async def query(self, prompt):
        self.queries.append(prompt)
        session = self.options.resume or self.options.session_id
        if self.options.system_prompt == claude_context.SUMMARY_SYSTEM:
            self.factory.summary_queries += 1
            summary = self.factory.summary
            if isinstance(summary, dict):
                # The legacy JSON envelope, still accepted when delivered as structured output.
                result = result_message(session_id=session, structured_output=summary)
            else:
                await self.messages.put(AssistantMessage(content=[TextBlock(text=summary)], model="sonnet"))
                result = result_message(session_id=session, result=summary)
            if self.factory.truncate_summary:
                result.stop_reason = "max_tokens"
            await self.messages.put(result)
            return
        self.factory.main_queries += 1
        number = self.factory.main_queries
        if number <= 2:
            await self.messages.put(AssistantMessage(content=[
                TextBlock(text=f"private-older-evidence-{number}"),
                ToolUseBlock(id=f"call-{number}", name="mcp__bello__read_file", input={"path": f"file-{number}"}),
            ], model="sonnet", usage={"input_tokens": 10, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 10_000}))
            await self.messages.put(UserMessage(content=[ToolResultBlock(tool_use_id=f"call-{number}", content=f"verbatim-result-{number}")]))
            result = result_message(session_id=session, result=None, is_error=True, terminal_reason="max_turns")
            result.subtype = "error_max_turns"
        else:
            result = result_message(session_id=session, result="finished")
        await self.messages.put(result)


class ContextFactory(FakeFactory):
    def __init__(self, *, summary=None, truncate_summary=False):
        super().__init__()
        self.summary = "The earlier inspection completed; continue from the retained results." if summary is None else summary
        self.truncate_summary = truncate_summary
        self.main_queries = 0
        self.summary_queries = 0

    def __call__(self, options):
        client = ContextClient(self, options)
        self.clients.append(client)
        return client


async def context_thread(instance, tmp_path, policy=None):
    await instance.request("initialize", {})
    await instance.request("thread/start", {
        "threadId": "thread-1", "model": "sonnet", "cwd": str(tmp_path),
        "effort": "high", "developerInstructions": "Keep the stable constraint.",
        "noNeedForContext": policy or {"enabled": True, "thresholdTokens": 1000, "retainPercent": 10},
    })


async def test_rollover_preserves_logical_thread_and_exact_whole_round(tmp_path):
    factory = ContextFactory()
    events = []
    instance = backend(tmp_path, factory, events)
    try:
        await context_thread(instance, tmp_path)
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Implement the original request."}]})
        result = await wait_completed(events)
        assert result["params"]["turn"]["status"] == "completed"
        assert factory.main_queries == 3
        assert factory.summary_queries == 1
        summaries = [c for c in factory.clients if c.options.system_prompt == claude_context.SUMMARY_SYSTEM]
        assert summaries[0].options.model == "sonnet"
        assert summaries[0].options.effort == "high"
        assert summaries[0].options.tools == [] and summaries[0].options.mcp_servers == {}
        assert summaries[0].options.resume is None
        main = [c for c in factory.clients if c.options.system_prompt != claude_context.SUMMARY_SYSTEM and c.queries]
        prompt = main[-1].queries[0]
        assert "private-older-evidence-1" not in prompt
        assert "private-older-evidence-2" in prompt
        assert "verbatim-result-2" in prompt
        assert "call-2" in prompt
        assert "Implement the original request." in prompt
        assert "untrusted historical data" in prompt
        assert "Keep the stable constraint." in main[-1].options.system_prompt
        record = instance._record("thread-1")
        assert record["id"] == "thread-1" and record["claudeSessionId"] != original
        assert record["contextState"]["archivedSessions"] == [original]
        archives = list((instance.state_dir / "context-archives").glob("*.json"))
        assert len(archives) == 1
        assert "private-older-evidence-1" in archives[0].read_text()
        assert all("private-older-evidence" not in json.dumps(event) for event in events if event["method"].startswith("thread/context"))
        # Two bounded slices, one summary, and the final response all count.
        assert result["params"]["turn"]["usage"]["input_tokens"] == 28
        assert len([e for e in events if e["method"] == "turn/started"]) == 1
        assert len([e for e in events if e["method"] == "turn/completed"]) == 1
        compact = next(e for e in events if e["method"] == "thread/contextCompacted")
        assert compact["params"]["effectiveThresholdTokens"] == 1000
    finally:
        await instance.stop()


@pytest.mark.parametrize("summary,truncated", [
    ({"status": "refused", "summary": "Cannot summarize."}, False),  # the legacy JSON envelope
    ({"status": "complete", "summary": ""}, False),
    ({"status": "complete", "summary": "partial"}, True),
    ("I'm sorry, but I can't summarize this conversation.", False),  # plain text: refusal only
    ("", False),  # plain text: empty
    ("A plain-text handoff that stopped at the output limit.", True),  # plain text: truncated
    ("x" * (claude_context.SUMMARY_MAX_CHARS + 1), False),  # over the maximum: rejected, never truncated
])
async def test_invalid_summary_retries_twice_without_changing_native_session(tmp_path, summary, truncated):
    factory = ContextFactory(summary=summary, truncate_summary=truncated)
    instance = backend(tmp_path, factory, events := [])
    try:
        await context_thread(instance, tmp_path)
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Complete task."}]})
        result = await wait_completed(events)
        assert result["params"]["turn"]["status"] == "completed"
        assert factory.summary_queries == 2
        assert instance._record("thread-1")["claudeSessionId"] == original
        assert instance._record("thread-1")["contextState"]["generation"] == 0
        assert not (instance.state_dir / "context-archives").exists()
        assert result["params"]["turn"]["usage"]["input_tokens"] == 35
    finally:
        await instance.stop()


def test_soft_tail_target_keeps_oversized_newest_round_and_original_request():
    state = claude_context.new_context()
    claude_context.begin_request(state, "original task")
    claude_context.append_input(state, "keep this correction", steering=True)
    claude_context.finish_round(state, "first")
    claude_context.begin_request(state, "new outer request")
    state["pending"].append({"role": "tool", "content": "x" * 10_000})
    claude_context.finish_round(state, "second")
    older, retained = claude_context.partition_rounds(state, {"thresholdTokens": 1000, "retainPercent": 10})
    assert len(older) == len(retained) == 1
    assert retained[0] == state["rounds"][-1]
    assert "original task" in claude_context.bootstrap_prompt(state)
    assert "keep this correction" in claude_context.bootstrap_prompt(state)


@pytest.mark.parametrize("value", [True, 0, -1, 50.01, 10**1000, float("nan"), float("inf")])
def test_invalid_retain_percent_is_rejected(value):
    with pytest.raises(AppServerError):
        claude_context.context_policy({"enabled": True, "retainPercent": value})


async def test_resume_cannot_change_context_policy(tmp_path):
    instance = backend(tmp_path, ContextFactory(), [])
    try:
        await context_thread(instance, tmp_path)
        with pytest.raises(AppServerError, match="cannot change noNeedForContext"):
            await instance.request("thread/resume", {"threadId": "thread-1", "noNeedForContext": {"enabled": False}})
    finally:
        await instance.stop()


async def test_incompatible_threshold_fails_before_a_model_query_instead_of_being_lowered(tmp_path):
    factory = ContextFactory()
    instance = backend(tmp_path, factory, events := [])
    try:
        await context_thread(instance, tmp_path, {"enabled": True, "thresholdTokens": 249000, "retainPercent": 10})
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Synthetic task."}]})
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "failed"
        assert "Configured context threshold 249000 exceeds the Claude model input capacity 190000" in completed["params"]["turn"]["error"]["message"]
        assert factory.main_queries == factory.summary_queries == 0
        assert instance._record("thread-1")["claudeSessionId"] == original
        assert instance._record("thread-1")["noNeedForContext"]["thresholdTokens"] == 249000
        assert instance._record("thread-1")["contextState"]["generation"] == 0
    finally:
        await instance.stop()


@pytest.mark.parametrize("reported_tokens,expected_summaries", [(90000, 0), (100000, 1)])
async def test_configured_threshold_ignores_sdk_automatic_trigger(tmp_path, reported_tokens, expected_summaries):
    class ExactThresholdClient(ContextClient):
        async def get_context_usage(self):
            return {"totalTokens": reported_tokens if self.factory.main_queries >= 2 and self.options.resume else 100,
                    "maxTokens": 1000000, "autoCompactThreshold": 50000}

    class ExactThresholdFactory(ContextFactory):
        def __call__(self, options):
            client = ExactThresholdClient(self, options)
            self.clients.append(client)
            return client

    factory = ExactThresholdFactory()
    instance = backend(tmp_path, factory, events := [])
    policy = {"enabled": True, "thresholdTokens": 100000, "retainPercent": 15}
    try:
        assert policy["thresholdTokens"] * policy["retainPercent"] / 100 == 15000
        await context_thread(instance, tmp_path, policy)
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Synthetic task."}]})
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "completed"
        assert factory.main_queries == 3 and factory.summary_queries == expected_summaries
        assert instance._record("thread-1")["noNeedForContext"] == policy
        checkpoints = [event for event in events if event["method"] == "thread/contextCompacted"]
        assert len(checkpoints) == expected_summaries
        for checkpoint in checkpoints:
            assert checkpoint["params"]["configuredThresholdTokens"] == 100000
            assert checkpoint["params"]["effectiveThresholdTokens"] == 100000
    finally:
        await instance.stop()


@pytest.mark.parametrize("steer_first", [False, True])
async def test_interrupt_cancels_summary_even_when_steering_is_queued_first(tmp_path, monkeypatch, steer_first):
    # This fake never ends an interrupted summary with a result; keep the
    # bounded wait for that missing final usage short.
    monkeypatch.setattr(claude_context, "SUMMARY_STOP_TIMEOUT_SECONDS", 0.3)

    class PausedSummaryClient(ContextClient):
        async def query(self, prompt):
            if self.options.system_prompt == claude_context.SUMMARY_SYSTEM:
                self.queries.append(prompt)
                self.factory.summary_queries += 1
                self.factory.summary_started.set()
                return
            await super().query(prompt)

    class PausedSummaryFactory(ContextFactory):
        def __init__(self):
            super().__init__()
            self.summary_started = asyncio.Event()

        def __call__(self, options):
            client = PausedSummaryClient(self, options)
            self.clients.append(client)
            return client

    factory = PausedSummaryFactory()
    instance = backend(tmp_path, factory, events := [])
    steering = None
    try:
        await context_thread(instance, tmp_path)
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Complete task."}]})
        await asyncio.wait_for(factory.summary_started.wait(), 1)
        if steer_first:
            steering = asyncio.create_task(instance.request("turn/steer", {"threadId": "thread-1", "expectedTurnId": "outer", "input": [{"type": "text", "text": "New correction."}]}))
            await asyncio.sleep(0)
        await asyncio.wait_for(instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}), 1)
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "interrupted"
        assert factory.main_queries == 2 and factory.summary_queries == 1
        assert instance._record("thread-1")["claudeSessionId"] == original
        summaries = [c for c in factory.clients if c.options.system_prompt == claude_context.SUMMARY_SYSTEM]
        # The submitted summary was explicitly interrupted; its final usage never
        # arrived, which the turn reports instead of an estimate.
        assert summaries[0].interrupts == 1
        assert summaries[0].disconnect_task is not None
        assert completed["params"]["turn"]["usageIncomplete"] is True
        assert not any(event["method"].startswith("thread/context") for event in events)
        if steering is not None:
            with pytest.raises(AppServerError):
                await asyncio.wait_for(steering, 1)
    finally:
        if steering is not None and not steering.done():
            steering.cancel()
            await asyncio.gather(steering, return_exceptions=True)
        await instance.stop()


async def test_candidate_connection_failure_keeps_original_checkpoint(tmp_path):
    class FailingCandidate(ContextClient):
        async def connect(self):
            if self.options.system_prompt != claude_context.SUMMARY_SYSTEM and not self.options.resume and self.factory.summary_queries:
                raise RuntimeError("fixture candidate connection failed")
            await super().connect()

    class FailingFactory(ContextFactory):
        def __call__(self, options):
            client = FailingCandidate(self, options)
            self.clients.append(client)
            return client

    factory = FailingFactory()
    instance = backend(tmp_path, factory, events := [])
    try:
        await context_thread(instance, tmp_path)
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Complete task."}]})
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "completed"
        assert instance._record("thread-1")["claudeSessionId"] == original
        assert instance._record("thread-1")["contextState"]["generation"] == 0
        assert not (instance.state_dir / "context-archives").exists()
    finally:
        await instance.stop()


def test_images_remain_native_image_blocks_in_quoted_history():
    state = claude_context.new_context()
    image = {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}
    claude_context.begin_request(state, "inspect evidence")
    claude_context.append_input(state, [image])
    claude_context.finish_round(state, "outer")
    value = claude_context.bootstrap_input(state)
    assert isinstance(value, list)
    assert "aGVsbG8=" not in value[0]["text"]
    assert value[1] == {"type": "image", "source": {"type": "base64", "data": "aGVsbG8=", "media_type": "image/png"}}
    assert "historical_image" in value[0]["text"]


async def test_pending_async_jobs_defer_rollover_without_cancellation(tmp_path):
    from types import SimpleNamespace

    factory = ContextFactory()
    instance = backend(tmp_path, factory, events := [])
    client = None
    try:
        await context_thread(instance, tmp_path)
        record = instance._record("thread-1")
        record["activeTurnId"] = "outer"
        record["hasSession"] = True
        record["contextState"]["rounds"] = [
            {"turnId": "outer", "messages": [{"role": "assistant", "content": "older evidence"}]},
            {"turnId": "outer", "messages": [{"role": "tool", "content": "new evidence"}]},
        ]
        coordinator = SimpleNamespace(pending=True)
        instance._async_batches[("thread-1", "outer")] = coordinator
        factory.main_queries = 2
        original = record["claudeSessionId"]
        client, prompt = await instance._connect_context_client(record, {"turnId": "outer", "effort": "high"})
        assert prompt is None and factory.summary_queries == 0
        assert record["claudeSessionId"] == original
        assert instance._async_batches[("thread-1", "outer")] is coordinator
    finally:
        instance._async_batches.clear()
        if client is not None:
            await client.disconnect()
        await instance.stop()


async def test_late_async_result_keeps_original_call_group_verbatim_through_rollover(tmp_path):
    factory = ContextFactory()
    instance = backend(tmp_path, factory, [])
    client = None
    try:
        await context_thread(instance, tmp_path)
        record = instance._record("thread-1")
        record["activeTurnId"] = "outer"
        record["hasSession"] = True
        state = record["contextState"]
        claude_context.begin_request(state, "original task")
        claude_context.append_input(state, "old unrelated evidence")
        claude_context.finish_round(state, "outer")
        call = {"role": "assistant", "content": [{"id": "batch-call", "name": "run_parallel_tools", "input": {"calls": [{"name": "read_file", "arguments": {"path": "exact-original-path"}}]}}]}
        state["pending"].append(call)
        claude_context.append_input(state, "batch-call: original placeholders")
        claude_context.finish_round(state, "outer", async_pending=True)
        claude_context.append_input(state, "intervening response while results pending")
        claude_context.finish_round(state, "outer", async_pending=True)
        assert len(state["rounds"]) == 2
        # take_ready has delivered the final job; its actual result remains an
        # unsent input while the adapter evaluates this checkpoint boundary.
        claude_context.append_input(state, "batch-call: final actual late result")
        factory.main_queries = 2
        client, prompt = await instance._connect_context_client(record, {"turnId": "outer", "effort": "high"})
        assert "old unrelated evidence" not in prompt
        assert "exact-original-path" in prompt
        assert "final actual late result" in prompt
        assert "intervening response while results pending" in prompt
        candidate = record["contextState"]
        assert candidate["openAsyncRoundStart"] == 0
        assert candidate["rounds"][0]["messages"][0] == call
        claude_context.finish_round(candidate, "outer", async_pending=False)
        assert len(candidate["rounds"]) == 1
        assert candidate["openAsyncRoundStart"] is None
        assert "final actual late result" in json.dumps(candidate["rounds"][0])
    finally:
        if client is not None:
            await client.disconnect()
        await instance.stop()


async def test_interrupt_during_steer_context_measurement_prevents_next_query(tmp_path, monkeypatch):
    from supervisor.runtime import claude as claude_module

    waiting = asyncio.Event()
    measuring = asyncio.Event()
    release_measurement = asyncio.Event()

    class PendingBatches:
        pending = True

        async def take_ready(self, *, wait=False):
            waiting.set()
            await asyncio.Event().wait()

        async def cancel(self):
            self.pending = False

    class MeasuringClient(FakeClient):
        async def get_context_usage(self):
            if self.factory.query_count:
                measuring.set()
                await release_measurement.wait()
            return {"totalTokens": 100, "maxTokens": 200_000}

        async def query(self, prompt):
            self.factory.query_count += 1
            await super().query(prompt)

    class MeasuringFactory(FakeFactory):
        query_count = 0

        def __call__(self, options):
            client = MeasuringClient(self, options)
            self.clients.append(client)
            return client

    monkeypatch.setattr(claude_module, "ClaudeAsyncBatches", PendingBatches)
    factory = MeasuringFactory([result_message()])
    instance = backend(tmp_path, factory, events := [])
    steering = interrupt = None
    try:
        await context_thread(instance, tmp_path)
        instance._record("thread-1")["asyncTools"] = True
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Complete task."}]})
        await asyncio.wait_for(waiting.wait(), 1)
        steering = asyncio.create_task(instance.request("turn/steer", {"threadId": "thread-1", "expectedTurnId": "outer", "input": [{"type": "text", "text": "New correction."}]}))
        await asyncio.wait_for(measuring.wait(), 1)
        interrupt = asyncio.create_task(instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}))
        await asyncio.sleep(0)
        release_measurement.set()
        await asyncio.wait_for(interrupt, 1)
        with pytest.raises(AppServerError):
            await asyncio.wait_for(steering, 1)
        result = await wait_completed(events)
        assert result["params"]["turn"]["status"] == "interrupted"
        assert factory.query_count == 1
        assert instance._record("thread-1")["contextState"]["openAsyncRoundStart"] is None
    finally:
        release_measurement.set()
        for task in (steering, interrupt):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (steering, interrupt) if task is not None), return_exceptions=True)
        await instance.stop()


class PersistenceCheckingClient(ContextClient):
    async def query(self, prompt):
        if self.options.system_prompt != claude_context.SUMMARY_SYSTEM:
            instance = self.factory.backend
            memory = instance._record("thread-1")
            disk = json.loads(instance._state_file.read_text())["threads"]["thread-1"]
            self.factory.query_states.append({
                "disk_session": disk["claudeSessionId"], "memory_session": memory["claudeSessionId"],
                "client_session": self.options.resume or self.options.session_id,
                "disk_generation": disk["contextState"]["generation"],
                "memory_generation": memory["contextState"]["generation"],
            })
        await super().query(prompt)


class PersistenceCheckingFactory(ContextFactory):
    def __init__(self):
        super().__init__()
        self.query_states = []
        self.backend = None

    def __call__(self, options):
        client = PersistenceCheckingClient(self, options)
        self.clients.append(client)
        return client


def assert_query_state_consistent(factory):
    assert factory.query_states
    for state in factory.query_states:
        assert state["disk_session"] == state["memory_session"] == state["client_session"]
        assert state["disk_generation"] == state["memory_generation"]


@pytest.mark.parametrize("fault", ["post_chmod", "post_replace", "pre_replace", "pre_fsync"])
async def test_checkpoint_write_fault_reconciles_session_before_continuing(tmp_path, monkeypatch, fault):
    from supervisor.runtime import claude as claude_module

    factory = PersistenceCheckingFactory()
    instance = backend(tmp_path, factory, events := [])
    factory.backend = instance
    fired = False
    original_replace, original_chmod, original_fsync = claude_module.os.replace, claude_module.os.chmod, claude_module.os.fsync

    def candidate():
        return instance._record("thread-1")["contextState"]["generation"] == 1

    def replace(source, destination):
        nonlocal fired
        if fault in {"pre_replace", "post_replace"} and not fired and Path(destination) == instance._state_file and candidate():
            fired = True
            if fault == "post_replace":
                original_replace(source, destination)
            raise OSError("synthetic checkpoint rename failure")
        return original_replace(source, destination)

    def chmod(path, mode, *args, **kwargs):
        nonlocal fired
        if fault == "post_chmod" and not fired and Path(path) == instance._state_file and candidate():
            fired = True
            raise OSError("synthetic post-replace chmod failure")
        return original_chmod(path, mode, *args, **kwargs)

    def fsync(descriptor):
        nonlocal fired
        if fault == "pre_fsync" and not fired and candidate():
            fired = True
            raise OSError("synthetic pre-replace fsync failure")
        return original_fsync(descriptor)

    try:
        await context_thread(instance, tmp_path)
        old_session = instance._record("thread-1")["claudeSessionId"]
        monkeypatch.setattr(claude_module.os, "replace", replace)
        monkeypatch.setattr(claude_module.os, "chmod", chmod)
        monkeypatch.setattr(claude_module.os, "fsync", fsync)
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Synthetic task."}]})
        completed = await wait_completed(events)
        assert fired and completed["params"]["turn"]["status"] == "completed"
        assert factory.main_queries == 3 and factory.summary_queries == 1
        assert_query_state_consistent(factory)
        assert not instance._state_persistence_uncertain
        record = instance._record("thread-1")
        committed = fault.startswith("post_")
        assert record["contextState"]["generation"] == int(committed)
        assert (record["claudeSessionId"] != old_session) is committed
        assert bool([e for e in events if e["method"] == "thread/contextCompacted"]) is committed
        if claude_module.os.name != "nt":
            assert instance._state_file.stat().st_mode & 0o777 == 0o600
        # A fresh backend must resume the identical committed identity, without
        # replaying either synthetic tool round after a handled storage error.
        resumed_factory = PersistenceCheckingFactory()
        resumed_factory.main_queries = 3
        resumed = backend(tmp_path, resumed_factory, resumed_events := [])
        resumed_factory.backend = resumed
        try:
            await resumed.request("thread/resume", {"threadId": "thread-1"})
            assert resumed._record("thread-1")["claudeSessionId"] == record["claudeSessionId"]
            await resumed.request("turn/start", {"threadId": "thread-1", "turnId": "after-restart", "input": [{"type": "text", "text": "Inspect and finish."}]})
            assert (await wait_completed(resumed_events))["params"]["turn"]["status"] == "completed"
            assert resumed_factory.main_queries == 4
            # A committed checkpoint is not repeated, and a failed one is not
            # retried by the resumed thread until the context has grown (the
            # persisted schedule); neither replays a completed tool.
            assert resumed_factory.summary_queries == 0
            assert record["contextState"]["failedCheckpoints"] == (0 if committed else 1)
            assert_query_state_consistent(resumed_factory)
        finally:
            await resumed.stop()
    finally:
        await instance.stop()


async def test_unreadable_checkpoint_outcome_blocks_queries_and_writes_until_reload(tmp_path, monkeypatch):
    from supervisor.runtime import claude as claude_module

    factory = PersistenceCheckingFactory()
    instance = backend(tmp_path, factory, events := [])
    factory.backend = instance
    original_replace, original_read = claude_module.os.replace, Path.read_bytes
    fired = False

    def replace(source, destination):
        nonlocal fired
        if not fired and Path(destination) == instance._state_file and instance._record("thread-1")["contextState"]["generation"] == 1:
            original_replace(source, destination)
            fired = True
            raise OSError("synthetic ambiguous publish")
        return original_replace(source, destination)

    def read_bytes(path):
        if fired and path == instance._state_file:
            raise OSError("synthetic unavailable readback")
        return original_read(path)

    try:
        await context_thread(instance, tmp_path)
        monkeypatch.setattr(claude_module.os, "replace", replace)
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Synthetic task."}]})
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "failed"
        assert instance._state_persistence_uncertain
        assert factory.main_queries == 2 and factory.summary_queries == 1
        published = instance._state_file.read_text()
        durable = json.loads(published)["threads"]["thread-1"]
        assert durable["contextState"]["generation"] == 1
        assert durable["turns"][-1]["status"] == "inProgress", "failure completion must not overwrite ambiguous durable state"
        with pytest.raises(AppServerError, match="commit status is unknown"):
            instance._persist()
        with pytest.raises(AppServerError, match="commit status is unknown"):
            await instance._query(factory.clients[-1], "must not query")
        with pytest.raises(AppServerError, match="commit status is unknown"):
            await instance._context_summary(instance._record("thread-1"), {"turnId": "outer"}, [], None)
        with pytest.raises(AppServerError, match="commit status is unknown"):
            await instance.request("turn/start", {"threadId": "thread-1", "turnId": "blocked", "input": [{"type": "text", "text": "Do not run."}]})
        with pytest.raises(AppServerError, match="commit status is unknown"):
            await instance.request("thread/resume", {"threadId": "thread-1"})
        assert instance._state_file.read_text() == published
        assert factory.main_queries == 2 and factory.summary_queries == 1

        monkeypatch.setattr(Path, "read_bytes", original_read)
        resumed_factory = PersistenceCheckingFactory()
        resumed_factory.main_queries = 3
        resumed = backend(tmp_path, resumed_factory, resumed_events := [])
        resumed_factory.backend = resumed
        try:
            await resumed.request("thread/resume", {"threadId": "thread-1"})
            assert not resumed._state_persistence_uncertain
            assert resumed._record("thread-1")["claudeSessionId"] == durable["claudeSessionId"]
            await resumed.request("turn/start", {"threadId": "thread-1", "turnId": "reloaded", "input": [{"type": "text", "text": "Inspect and stop."}]})
            assert (await wait_completed(resumed_events))["params"]["turn"]["status"] == "completed"
            assert resumed_factory.main_queries == 4 and resumed_factory.summary_queries == 0
            assert_query_state_consistent(resumed_factory)
        finally:
            await resumed.stop()
    finally:
        await instance.stop()


@pytest.mark.parametrize("boundary", ["replace", "notification"])
async def test_cancellation_after_checkpoint_commit_keeps_new_session_for_resume(tmp_path, monkeypatch, boundary):
    from supervisor.runtime import claude as claude_module

    factory = PersistenceCheckingFactory()
    instance = backend(tmp_path, factory, events := [])
    factory.backend = instance
    original_replace, original_emit = claude_module.os.replace, instance.emit
    fired = False

    def replace(source, destination):
        nonlocal fired
        original_replace(source, destination)
        if boundary == "replace" and not fired and Path(destination) == instance._state_file and instance._record("thread-1")["contextState"]["generation"] == 1:
            fired = True
            raise asyncio.CancelledError()

    async def emit(event):
        nonlocal fired
        if boundary == "notification" and not fired and event["method"] == "thread/contextCompacted":
            fired = True
            raise asyncio.CancelledError()
        await original_emit(event)

    try:
        await context_thread(instance, tmp_path)
        old_session = instance._record("thread-1")["claudeSessionId"]
        monkeypatch.setattr(claude_module.os, "replace", replace)
        instance.emit = emit
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer", "input": [{"type": "text", "text": "Synthetic task."}]})
        completed = await wait_completed(events)
        assert fired and completed["params"]["turn"]["status"] == "interrupted"
        assert factory.main_queries == 2 and factory.summary_queries == 1
        disk = json.loads(instance._state_file.read_text())["threads"]["thread-1"]
        assert disk["claudeSessionId"] == instance._record("thread-1")["claudeSessionId"] != old_session
        assert disk["contextState"]["generation"] == 1
        assert disk["contextState"]["bootstrapPending"]
        resumed_factory = PersistenceCheckingFactory()
        resumed_factory.main_queries = 3
        resumed = backend(tmp_path, resumed_factory, resumed_events := [])
        resumed_factory.backend = resumed
        try:
            await resumed.request("thread/resume", {"threadId": "thread-1"})
            await resumed.request("turn/start", {"threadId": "thread-1", "turnId": "reloaded", "input": [{"type": "text", "text": "Inspect and stop."}]})
            assert (await wait_completed(resumed_events))["params"]["turn"]["status"] == "completed"
            assert resumed_factory.main_queries == 4 and resumed_factory.summary_queries == 0
            assert_query_state_consistent(resumed_factory)
            prompt = next(client.queries[0] for client in resumed_factory.clients if client.queries)
            assert "private-older-evidence-1" not in prompt
            assert "verbatim-result-2" in prompt
        finally:
            await resumed.stop()
    finally:
        await instance.stop()
