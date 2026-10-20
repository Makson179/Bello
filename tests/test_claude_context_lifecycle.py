"""Claude no-need-for-context lifecycle: permissions, resume, terminal state and anti-thrash (offline).

Synthetic fakes only; every case drives ClaudeBackend itself. The scripted SDK
clients come from tests/test_runtime_claude_context_summary.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from supervisor.runtime import claude_context
from tests.test_runtime_claude import backend, wait_completed
from tests.test_runtime_claude_context_summary import (BARE, SummaryScriptClient, SummaryScriptFactory, answer,
                                                       configured_thread, context_events, run_turn)

STATE_FIELDS = {"summary", "rounds", "pending", "activeRequest", "originalRequest", "latestSteering", "steering",
                "generation", "archivedSessions", "bootstrapPending", "openAsyncRoundStart"}


def completions(events):
    return [event["params"]["turn"] for event in events if event["method"] == "turn/completed"]


# --- NNC adds no sandbox, archive or escalation restriction -----------------------------


async def test_context_checkpoints_change_no_permission_root_or_approval_option(tmp_path):
    factory = SummaryScriptFactory([answer()])
    instance = backend(tmp_path, factory, [])
    try:
        await instance.request("initialize", {})
        records = {}
        for name, policy in (("off", {"enabled": False}), ("on", {"enabled": True, "thresholdTokens": 1000})):
            await instance.request("thread/start", {
                "threadId": name, "model": "sonnet", "cwd": str(tmp_path), "effort": "high", "asyncTools": True,
                "developerInstructions": "Keep the stable constraint", "noNeedForContext": policy,
                "tools": [{"name": "read_file", "description": "Read one file.", "parameters": {
                    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
            })
            records[name] = instance._record(name)
        off = instance._options(records["off"], {"effort": "high"})
        on = instance._options(records["on"], {"effort": "high"})
        # Everything that decides what the agent may do is identical.
        differing = {field.name for field in dataclasses.fields(off)
                     if getattr(off, field.name) != getattr(on, field.name)}
        # The two sessions differ in identity and in how Bello keeps each round
        # indivisible (one SDK turn per round, a PreCompact hook, hooks enabled).
        assert differing <= {"session_id", "max_turns", "hooks", "settings", "mcp_servers"}
        for field in ("permission_mode", "allowed_tools", "disallowed_tools", "tools", "cwd", "add_dirs", "env",
                      "setting_sources", "plugins", "skills", "can_use_tool", "extra_args", "system_prompt",
                      "strict_mcp_config", "model", "effort"):
            assert getattr(on, field) == getattr(off, field), field
        assert on.permission_mode == "dontAsk" and on.cwd == off.cwd == str(tmp_path)
        assert not getattr(on, "add_dirs", None)  # no extra readable root, none withheld either
        assert set(on.mcp_servers) == set(off.mcp_servers) == {"bello"}
        assert json.loads(on.settings) == {**json.loads(off.settings), "disableAllHooks": False}
        assert set(on.hooks) == {"PreCompact"} and off.hooks is None and on.max_turns == 1
        # The private archive is storage, not a policy: nothing in the options names it.
        assert "context-archives" not in repr(on.env) + on.settings + on.system_prompt
    finally:
        await instance.stop()


async def test_the_private_archive_may_live_inside_the_workspace_without_changing_the_next_launch(tmp_path):
    factory = SummaryScriptFactory([answer()])
    instance = backend(tmp_path, factory, events := [])  # its state directory is inside the thread's workspace
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        archive = instance.state_dir / "context-archives"
        assert turn["status"] == "completed" and instance._record("thread-1")["contextState"]["generation"] == 1
        assert archive.is_dir() and tmp_path in archive.parents  # no guard rejects the exposed root
        main = [client.options for client in factory.clients if not client.summarizer]
        assert len(main) >= 2 and len({(o.permission_mode, o.cwd, tuple(o.allowed_tools), tuple(o.disallowed_tools),
                                        tuple(o.setting_sources)) for o in main}) == 1
    finally:
        await instance.stop()


# --- Resume: the persisted state decides, the native session is never fed twice -------------


async def test_unconfirmed_bootstrap_in_an_existing_native_session_continues_in_a_clean_one(tmp_path):
    factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 100)
    instance = backend(tmp_path, factory, events := [])
    try:
        await configured_thread(instance, tmp_path)
        record = instance._record("thread-1")
        # A committed checkpoint whose native session exists (the CLI reported it) but
        # never confirmed the bootstrap: the process stopped, or the turn failed first.
        state = claude_context.new_context()
        state.update(summary="The earlier inspection completed.", generation=1, bootstrapPending=True,
                     originalRequest="the original task", activeRequest="the original task",
                     rounds=[{"turnId": "old", "messages": [{"role": "tool", "content": "retained-result"}]}])
        record.update(contextState=state, hasSession=True)
        old = record["claudeSessionId"]
        instance._persist()
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Continue."}]})
        assert (await wait_completed(events))["params"]["turn"]["status"] == "completed"
        record = instance._record("thread-1")
        assert record["claudeSessionId"] != old and old in record["contextState"]["archivedSessions"]
        [(session, prompt)] = factory.main_prompts
        assert session == record["claudeSessionId"] and "retained-result" in prompt and "the original task" in prompt
        client = next(c for c in factory.clients if c.queries and not c.summarizer)
        assert client.options.resume is None and client.options.session_id == record["claudeSessionId"]
        assert record["contextState"]["bootstrapPending"] is False and factory.summary_prompts == []
        durable = json.loads(instance._state_file.read_text())["threads"]["thread-1"]
        assert durable["claudeSessionId"] == record["claudeSessionId"]
    finally:
        await instance.stop()


async def test_a_confirmed_session_is_resumed_not_rolled_over(tmp_path):
    factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 100)
    instance = backend(tmp_path, factory, events := [])
    try:
        await configured_thread(instance, tmp_path)
        record = instance._record("thread-1")
        record.update(hasSession=True)
        record["contextState"].update(originalRequest="task", activeRequest="task", rounds=[])
        old = record["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Continue."}]})
        assert (await wait_completed(events))["params"]["turn"]["status"] == "completed"
        record = instance._record("thread-1")
        assert record["claudeSessionId"] == old and not record["contextState"]["archivedSessions"]
        assert factory.clients[0].options.resume == old
    finally:
        await instance.stop()


async def test_a_restart_closes_an_async_round_that_was_open_when_the_process_stopped(tmp_path):
    factory = SummaryScriptFactory([answer()])
    instance = backend(tmp_path, factory, [])
    try:
        await configured_thread(instance, tmp_path)
        state = instance._record("thread-1")["contextState"]
        claude_context.begin_request(state, "original task")
        claude_context.append_input(state, "the batch call")
        claude_context.finish_round(state, "outer", async_pending=True)  # a batch was still running
        assert state["openAsyncRoundStart"] == 0
        instance._persist()
    finally:
        await instance.stop()
    resumed = backend(tmp_path, SummaryScriptFactory([answer()]), [])
    try:
        # Late results died with the old process: later rounds must not be swallowed by this one.
        assert resumed._record("thread-1")["contextState"]["openAsyncRoundStart"] is None
        state = resumed._record("thread-1")["contextState"]
        claude_context.append_input(state, "the next request")
        claude_context.finish_round(state, "next")
        assert [round_["turnId"] for round_ in state["rounds"]] == ["outer", "next"]
    finally:
        await resumed.stop()


# --- Every turn ends exactly once ------------------------------------------------------------


class FailingMeasureFactory(SummaryScriptFactory):
    """The main session's context measurement fails once the first round is over."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_measure = False

    def __call__(self, options):
        client = SummaryScriptClient(self, options)
        original = client.get_context_usage

        async def get_context_usage():
            if self.fail_measure and not client.summarizer and options.resume:
                raise RuntimeError("synthetic measurement failure")
            return await original()

        client.get_context_usage = get_context_usage
        self.clients.append(client)
        return client


@pytest.mark.parametrize("scenario", ["required_checkpoint_fails", "measurement_fails", "notification_fails"])
async def test_a_checkpoint_problem_ends_the_turn_exactly_once_and_leaves_the_thread_usable(tmp_path, scenario):
    tokens = {2: 196_000} if scenario == "required_checkpoint_fails" else None
    factory = FailingMeasureFactory([answer("```json\n" + BARE)], rounds=2,
                                    curve=(lambda queries: tokens.get(queries, 100)) if tokens else None)
    factory.fail_measure = scenario == "measurement_fails"
    instance = backend(tmp_path, factory, events := [])
    try:
        if scenario == "notification_fails":
            factory.scripts = [answer()]
            original_emit = instance.emit

            async def flaky_emit(event):
                if event["method"] == "thread/contextCompacted":
                    raise RuntimeError("synthetic notification failure")
                await original_emit(event)

            instance.emit = flaky_emit
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert len(completions(events)) == 1 and instance._record("thread-1").get("activeTurnId") is None
        if scenario == "notification_fails":
            assert turn["status"] == "completed"  # the checkpoint was committed; its telemetry is best effort
            assert instance._record("thread-1")["contextState"]["generation"] == 1
        else:
            assert turn["status"] == "failed" and turn["error"]["message"]
            assert instance._record("thread-1")["claudeSessionId"] == original
        # The same thread accepts and finishes another request: nothing is stuck.
        factory.fail_measure = False
        factory.rounds = 0
        events.clear()
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "again",
                                              "input": [{"type": "text", "text": "Continue."}]})
        async with asyncio.timeout(5):
            while not completions(events):
                await asyncio.sleep(0)
        assert len(completions(events)) == 1
    finally:
        await instance.stop()


async def test_a_turn_can_never_outlive_the_task_that_drives_it(tmp_path, monkeypatch):
    factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 100)
    instance = backend(tmp_path, factory, events := [])

    async def vanishing_result(*_args, **_kwargs):
        return True  # claims the turn is finished without ever completing it

    monkeypatch.setattr(instance, "_handle_message", vanishing_result)
    try:
        await configured_thread(instance, tmp_path)
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Task."}]})
        turn = (await wait_completed(events))["params"]["turn"]
        assert turn["status"] == "failed" and "without a result" in turn["error"]["message"]
        assert len(completions(events)) == 1 and instance._record("thread-1").get("activeTurnId") is None
    finally:
        await instance.stop()


# --- Anti-thrash: a checkpoint is not repeated on the very next round -------------------------


class FreshBaselineClient(SummaryScriptClient):
    async def get_context_usage(self):
        if not self.summarizer and not self.options.resume and self.factory.main_queries >= 2:
            return {"totalTokens": self.factory.fresh_baseline, "maxTokens": self.factory.max_tokens}
        return await super().get_context_usage()


class FreshBaselineFactory(SummaryScriptFactory):
    fresh_baseline = 0

    def __call__(self, options):
        client = FreshBaselineClient(self, options)
        self.clients.append(client)
        return client


async def test_a_checkpoint_that_lands_just_below_the_threshold_is_not_repeated_every_round(tmp_path):
    tokens = {2: 150_000, 3: 104_000, 4: 108_000, 5: 130_000}
    factory = FreshBaselineFactory([answer(), answer()], rounds=5, curve=lambda queries: tokens.get(queries, 100))
    factory.fresh_baseline = 99_000
    instance = backend(tmp_path, factory, events := [])
    policy = {"enabled": True, "thresholdTokens": 100_000, "retainPercent": 10}
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events, policy)
        assert turn["status"] == "completed"
        assert [event["method"] for event in context_events(events)] == ["thread/contextCompacted"] * 2
        compacted = [event["params"] for event in context_events(events)]
        # Rounds 3 and 4 were above the threshold but below the committed bar: no summary was paid.
        assert [params["previousTokens"] for params in compacted] == [150_000, 130_000]
        assert len(factory.summary_prompts) == 2
        first = compacted[0]
        assert first["freshTokens"] < 100_000
        durable = json.loads(instance._state_file.read_text())["threads"]["thread-1"]["contextState"]
        assert durable["generation"] == 2 and durable["failedCheckpoints"] == 0
        assert durable["nextCheckTokens"] == max(100_000, compacted[1]["freshTokens"] + 10_000)
    finally:
        await instance.stop()


@pytest.mark.parametrize("case,category", [
    ("summary_larger_than_the_context", "not_reducing"),
    ("fresh_session_not_smaller", "candidate_too_large"),
    ("fresh_session_above_the_threshold", "not_reducing"),
])
async def test_a_checkpoint_that_does_not_reduce_the_context_is_rejected_and_the_original_kept(tmp_path, case, category):
    policy = {"enabled": True, "thresholdTokens": 100_000, "retainPercent": 10}
    if case == "summary_larger_than_the_context":
        # A 45,000-character handoff (within the maximum) is about 11,000 tokens: more than the
        # 5,000-token context it would replace.
        policy["thresholdTokens"] = 1_000
        script = answer("The earlier work is summarized in detail: " + "detail " * 6_400)
        tokens, baseline = 5_000, 100
    else:
        script = answer()
        tokens = 150_000
        baseline = 160_000 if case == "fresh_session_not_smaller" else 100_500
    factory = FreshBaselineFactory([script], rounds=2, curve=lambda queries: tokens if queries >= 2 else 100)
    factory.fresh_baseline = baseline
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events, policy)
        assert turn["status"] == "completed"
        [failed] = context_events(events)
        assert failed["method"] == "thread/contextCompactionFailed" and failed["params"]["category"] == category
        assert failed["params"]["stage"] == "replacement" and failed["params"]["originalContextContinues"] is True
        record = instance._record("thread-1")
        assert record["claudeSessionId"] == original and record["contextState"]["generation"] == 0
        assert record["contextState"]["failedCheckpoints"] == 1 and not record["contextState"]["archivedSessions"]
        assert not (instance.state_dir / "context-archives").exists()
        assert factory.main_prompts[-1][0] == original, "the next request continued the original session"
    finally:
        await instance.stop()


# --- The pure schedule -------------------------------------------------------------------------


def test_checkpoint_schedule_grows_by_a_tenth_of_the_threshold_and_doubles_per_failure():
    state = claude_context.new_context()
    assert claude_context.checkpoint_due(state, 1000, 1000, required=False)
    assert not claude_context.checkpoint_due(state, 1000, 999, required=False)
    receipts = [claude_context.note_checkpoint_failure(state, 1000, projected) for projected in (5000, 5150, 5400)]
    assert [r["retryAtTokens"] for r in receipts] == [5100, 5350, None]
    assert [r["consecutiveFailures"] for r in receipts] == [1, 2, 3]
    assert [r["thresholdRetriesExhausted"] for r in receipts] == [False, False, True]
    # Exhausted: threshold-driven retries stop, but a required checkpoint is still due.
    assert not claude_context.checkpoint_due(state, 1000, 10**9, required=False)
    assert claude_context.checkpoint_due(state, 1000, 10**9, required=True)
    state = claude_context.new_context()
    claude_context.note_checkpoint_failure(state, 1000, 5000)
    assert not claude_context.checkpoint_due(state, 1000, 5099, required=False)
    assert claude_context.checkpoint_due(state, 1000, 5100, required=False)


def test_a_commit_resets_the_schedule_and_asks_for_growth_beyond_the_fresh_session():
    state = claude_context.new_context()
    claude_context.note_checkpoint_failure(state, 1000, 5000)
    claude_context.note_checkpoint_commit(state, 1000, 300)
    assert (state["failedCheckpoints"], state["nextCheckTokens"]) == (0, 1000)
    claude_context.note_checkpoint_commit(state, 1000, 950)
    assert state["nextCheckTokens"] == 1050
    assert not claude_context.checkpoint_due(state, 1000, 1049, required=False)
    assert claude_context.checkpoint_due(state, 1000, 1050, required=False)


def test_a_state_saved_without_the_schedule_fields_has_no_extra_bar():
    state = {key: value for key, value in claude_context.new_context().items()
             if key not in {"failedCheckpoints", "nextCheckTokens"}}
    assert claude_context.checkpoint_due(state, 1000, 1000, required=False)
    claude_context.note_checkpoint_failure(state, 1000, 2000)
    assert (state["failedCheckpoints"], state["nextCheckTokens"]) == (1, 2100)


@pytest.mark.parametrize("damaged", [None, "3", -4, True, 2.5, [1], {"x": 1}])
def test_damaged_schedule_fields_never_break_the_checkpoint_decision(damaged):
    state = claude_context.new_context()
    state.update(failedCheckpoints=damaged, nextCheckTokens=damaged)
    assert claude_context.failed_checkpoints(state) == 0
    assert claude_context.checkpoint_due(state, 1000, 1000, required=False)
    assert claude_context.note_checkpoint_failure(state, 1000, 2000)["consecutiveFailures"] == 1


def test_new_context_state_keeps_its_documented_shape():
    assert STATE_FIELDS | {"failedCheckpoints", "nextCheckTokens"} == set(claude_context.new_context())
