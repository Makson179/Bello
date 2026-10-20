"""Summary creation -> accepted checkpoint -> native-session continuation (synthetic fakes).

The scripted SDK fakes model context size as it grows: every main query adds
its prompt plus one round of tool output to the native session's token count,
and a fresh (bootstrapped) session starts from its bootstrap. That lets these
tests check what a checkpoint must achieve: the active context really shrinks,
the original instructions, retained whole rounds, tool-call/result bindings
and usage survive, later checkpoints carry the previous handoff forward, and a
checkpoint never re-triggers itself on the next round.
"""

from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from supervisor.runtime import claude_context
from supervisor.runtime.claude import ClaudeBackend
from tests.test_runtime_claude import AUTH, result_message, wait_completed
from tests.test_runtime_claude_context_summary import (HANDOFF, SummaryScriptClient, SummaryScriptFactory, answer,
                                                       context_events)

DEVELOPER = "Keep the stable constraint DEV-INSTRUCTION-7."
TASK = "Implement ORIGINAL-TASK-42 in the parser."


def numbered_handoff(number: int) -> str:
    return HANDOFF.replace("## Critical data\n", f"## Critical data\nHANDOFF-{number}\n")


class GrowthClient(SummaryScriptClient):
    """Main sessions grow by their prompt plus one large tool round per query."""

    async def get_context_usage(self):
        if self.summarizer:
            return dict(self.factory.summary_usage)
        session = self.options.resume or self.options.session_id
        return {"totalTokens": self.factory.session_tokens.get(session, self.factory.base_tokens),
                "maxTokens": self.factory.max_tokens}

    async def query(self, prompt):
        if self.summarizer:
            await super().query(prompt)
            return
        if not isinstance(prompt, str):
            prompt = [message async for message in prompt]
        session = self.options.resume or self.options.session_id
        self.queries.append(prompt)
        self.factory.main_prompts.append((session, prompt))
        self.factory.main_queries += 1
        number = self.factory.main_queries
        tokens = self.factory.session_tokens.get(session, self.factory.base_tokens)
        self.factory.main_sizes.append((session, tokens))
        added = len(prompt if isinstance(prompt, str) else json.dumps(prompt)) // 4
        if number <= self.factory.rounds:
            output = f"verbatim-result-{number} " + "evidence " * (self.factory.round_tokens * 4 // 9)
            await self.messages.put(AssistantMessage(content=[
                TextBlock(text=f"private-older-evidence-{number}"),
                ToolUseBlock(id=f"call-{number}", name="mcp__bello__read_file", input={"path": f"file-{number}"}),
            ], model="sonnet"))
            await self.messages.put(UserMessage(content=[ToolResultBlock(tool_use_id=f"call-{number}",
                                                                         content=output)]))
            limit = result_message(session_id=session, result=None, is_error=True, terminal_reason="max_turns")
            limit.subtype = "error_max_turns"
            await self.messages.put(limit)
            added += len(output) // 4 + 40
        else:
            await self.messages.put(result_message(session_id=session, result="finished"))
        self.factory.session_tokens[session] = tokens + added


class GrowthFactory(SummaryScriptFactory):
    def __init__(self, scripts, *, rounds, round_tokens, base_tokens=3_000):
        super().__init__(scripts, rounds=rounds)
        self.round_tokens = round_tokens
        self.base_tokens = base_tokens
        self.session_tokens: dict[str, int] = {}
        self.main_sizes: list[tuple[str, int]] = []

    def __call__(self, options):
        client = GrowthClient(self, options)
        self.clients.append(client)
        return client


def make_backend(tmp_path, factory, events):
    async def emit(event):
        events.append(event)

    calls = []

    async def tool(request):
        calls.append(request["callId"])
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    instance = ClaudeBackend(tmp_path / "claude-state", emit, tool_handler=tool, client_factory=factory,
                             auth_probe=lambda: dict(AUTH), environment={},
                             cli_path=tmp_path / "official-claude-cli-fixture")
    return instance, calls


async def start(instance, tmp_path, policy):
    await instance.request("initialize", {})
    await instance.request("thread/start", {
        "threadId": "thread-1", "model": "sonnet", "cwd": str(tmp_path), "effort": "max",
        "developerInstructions": DEVELOPER, "noNeedForContext": policy,
        "tools": [{"name": "read_file", "description": "Read one file.", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
    })
    return instance._record("thread-1")["claudeSessionId"]


async def run(instance, events, text=TASK):
    await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                          "input": [{"type": "text", "text": text}]})
    async with asyncio.timeout(10):
        completed = await wait_completed(events)
    return completed["params"]["turn"]


async def test_checkpoint_then_native_continuation_keeps_instructions_tail_bindings_and_usage(tmp_path):
    factory = SummaryScriptFactory([answer()], rounds=2)
    events: list = []
    instance, _calls = make_backend(tmp_path, factory, events)
    try:
        original = await start(instance, tmp_path, {"enabled": True, "thresholdTokens": 1000, "retainPercent": 10})
        turn = await run(instance, events)
        assert turn["status"] == "completed"
        record = instance._record("thread-1")
        state = record["contextState"]
        assert state["generation"] == 1 and state["summary"] == HANDOFF
        assert state["archivedSessions"] == [original] and record["claudeSessionId"] != original
        # The retained tail is the newest WHOLE round: assistant call + its tool result.
        [kept] = state["rounds"]
        roles = [message["role"] for message in kept["messages"]]
        assert roles == ["assistant", "tool"]
        call = kept["messages"][0]["content"][1]
        result = kept["messages"][1]["content"][0]
        assert call["id"] == "call-2" and result["tool_use_id"] == "call-2" and result["content"] == "verbatim-result-2"
        # The continuation is a fresh native session started from the bootstrap.
        fresh = next(client for client in factory.clients if client.options.session_id == record["claudeSessionId"])
        assert fresh.options.resume is None
        assert DEVELOPER in fresh.options.system_prompt  # original instructions are not summarized away
        assert "mcp__bello__read_file" in fresh.options.allowed_tools
        session, bootstrap = factory.main_prompts[-1]
        assert session == record["claudeSessionId"]
        assert HANDOFF in bootstrap and TASK in bootstrap and "untrusted historical data" in bootstrap
        assert '"tool_use_id":"call-2"' in bootstrap and "verbatim-result-2" in bootstrap
        assert "private-older-evidence-1" not in bootstrap and "verbatim-result-1" not in bootstrap
        # The pre-checkpoint context stays recoverable in the private archive.
        [archive] = (instance.state_dir / "context-archives").glob("*.json")
        archived = json.loads(archive.read_text())
        assert archived["nativeSessionId"] == original and "verbatim-result-1" in json.dumps(archived)
        # Usage: two bounded slices, the summary session and the continuation, each once.
        assert turn["usage"]["input_tokens"] == 7 + 7 + 100 + 7
    finally:
        await instance.stop()


async def test_long_context_repeated_checkpoints_shrink_context_and_carry_the_handoff(tmp_path):
    scripts = [answer(numbered_handoff(number)) for number in range(1, 9)]
    factory = GrowthFactory(scripts, rounds=14, round_tokens=4_000)
    events: list = []
    instance, _calls = make_backend(tmp_path, factory, events)
    policy = {"enabled": True, "thresholdTokens": 20_000, "retainPercent": 10}
    try:
        await start(instance, tmp_path, policy)
        turn = await run(instance, events)
        assert turn["status"] == "completed"
        compacted = [event["params"] for event in context_events(events)]
        assert len(compacted) >= 3, "long histories checkpoint repeatedly"
        assert all(event.get("category") is None for event in compacted), "no failed checkpoint"
        assert [event["generation"] for event in compacted] == list(range(1, len(compacted) + 1))
        assert len(factory.summary_prompts) == len(compacted), "exactly one summary per checkpoint"
        for event in compacted:
            assert event["freshTokens"] < event["previousTokens"]
            assert event["freshTokens"] < policy["thresholdTokens"], "a checkpoint must not re-trigger itself"
            assert event["summarizedRounds"] >= 1 and event["retainedRounds"] >= 1
        # Each later summary receives the previous handoff and the original task.
        for number, prompt in enumerate(factory.summary_prompts[1:], start=2):
            data = json.loads(prompt.split("\n")[1])
            assert data["previousSummary"] == numbered_handoff(number - 1)
            assert data["originalTask"] == TASK
            rounds = json.dumps(data["olderCompletedRounds"])
            assert f"HANDOFF-{number - 1}" not in rounds  # summaries are not duplicated into the rounds
        # The continuing session sees only the latest handoff and the newest rounds.
        state = instance._record("thread-1")["contextState"]
        assert state["summary"] == numbered_handoff(len(compacted))
        bootstraps = [prompt for session, prompt in factory.main_prompts
                      if isinstance(prompt, str) and "context checkpoint" in prompt]
        assert len(bootstraps) == len(compacted)
        for number, bootstrap in enumerate(bootstraps, start=1):
            assert numbered_handoff(number) in bootstrap and TASK in bootstrap
            assert all(f"HANDOFF-{other}" not in bootstrap for other in range(1, number))
            assert "private-older-evidence-1\"" not in bootstrap
        # Every model request ran below the threshold: a checkpoint always came first.
        sizes = [tokens for _session, tokens in factory.main_sizes]
        assert max(sizes) < policy["thresholdTokens"]
        # Rounds accumulate between checkpoints: never two summaries in consecutive rounds.
        assert all(event["summarizedRounds"] >= 2 for event in compacted[1:])
        assert factory.main_queries == factory.rounds + 1  # no slice was replayed
        assert turn["usage"]["input_tokens"] == 7 * (factory.rounds + 1) + 100 * len(compacted)
    finally:
        await instance.stop()


async def test_a_checkpoint_that_would_not_drop_below_the_threshold_is_not_committed(tmp_path):
    # The original task alone is larger than the threshold: any bootstrap would
    # start above it and summarize again on the very next round.
    factory = GrowthFactory([answer()], rounds=3, round_tokens=4_000)
    events: list = []
    instance, _calls = make_backend(tmp_path, factory, events)
    policy = {"enabled": True, "thresholdTokens": 6_000, "retainPercent": 10}
    try:
        original = await start(instance, tmp_path, policy)
        turn = await run(instance, events, text=TASK + " " + "x" * 30_000)
        assert turn["status"] == "completed"
        failures = [event["params"] for event in context_events(events)]
        assert failures and all(item["category"] == "not_reducing" for item in failures)
        assert all(item["originalContextContinues"] for item in failures)
        record = instance._record("thread-1")
        assert record["claudeSessionId"] == original and record["contextState"]["generation"] == 0
        # Backoff: the next attempt needs real growth, so summaries are not paid every round.
        assert len(factory.summary_prompts) == len(failures) < factory.rounds
    finally:
        await instance.stop()


async def test_a_saved_checkpoint_without_the_schedule_fields_resumes_and_bootstraps(tmp_path):
    """A durable context state saved before the checkpoint schedule existed stays usable."""
    factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 100)
    events: list = []
    instance, _calls = make_backend(tmp_path, factory, events)
    try:
        await start(instance, tmp_path, {"enabled": True, "thresholdTokens": 1000, "retainPercent": 10})
        record = instance._record("thread-1")
        legacy_state = claude_context.new_context()
        legacy_state.update({
            "summary": "PRE-UPGRADE SUMMARY: the earlier inspection completed.",
            "rounds": [{"turnId": "old", "messages": [
                {"role": "assistant", "content": [{"id": "call-9", "name": "mcp__bello__read_file",
                                                   "input": {"path": "a"}}]},
                {"role": "tool", "content": [{"tool_use_id": "call-9", "content": "verbatim-result-9",
                                              "is_error": False}]}]}],
            "originalRequest": "the original pre-upgrade task", "activeRequest": "the original pre-upgrade task",
            "generation": 3, "archivedSessions": ["a", "b", "c"], "bootstrapPending": True,
        })
        for key in ("failedCheckpoints", "nextCheckTokens"):
            legacy_state.pop(key)
        record["contextState"] = legacy_state
        instance._persist()
    finally:
        await instance.stop()
    resumed_factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 100)
    resumed_events: list = []
    resumed, _calls = make_backend(tmp_path, resumed_factory, resumed_events)
    try:
        await resumed.request("thread/resume", {"threadId": "thread-1"})
        turn = await run(resumed, resumed_events, text="Continue.")
        assert turn["status"] == "completed"
        _session, prompt = resumed_factory.main_prompts[0]
        assert "PRE-UPGRADE SUMMARY: the earlier inspection completed." in prompt
        assert "verbatim-result-9" in prompt and "the original pre-upgrade task" in prompt
        assert resumed._record("thread-1")["contextState"]["generation"] == 3
        assert resumed_factory.summary_prompts == []
    finally:
        await resumed.stop()
