"""Claude summary acceptance contract, through the production path (offline).

Every case drives ClaudeBackend's real summarizer code (connection, account,
model and MCP validation, input admission, query dispatch, stream observation,
terminal inspection, usage accounting and the bounded stop) with scripted SDK
fakes whose frames come from the pinned SDK's message parser. No CLI process,
network, credential or model request is involved.

* end_turn and stop_sequence end a complete answer; an absent optional stop or
  terminal reason is not a rejection by itself (positive controls);
* explicit model_context_window_exceeded and unknown stop reasons are rejected;
* useful plain text is accepted as written, without headings, labels or JSON;
  refusal-only output is rejected even when padded with headings, while a
  useful handoff that quotes a refusal is kept;
* admission reserves the whole 64,000-token combined cap plus a margin;
* terminal usage already delivered survives a failing interrupt, once.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from claude_agent_sdk import CLIConnectionError

from supervisor.runtime import claude_context
from supervisor.runtime.claude import _CheckpointFailure
from tests.test_runtime_claude import backend, wait_completed
from tests.test_runtime_claude_context_summary import (ABORTED, BARE, FENCED, GATE, GOOD, HANDOFF, HANG,
                                                       REFUSAL_PADDED_WITH_HEADINGS, SECRET, SummaryScriptClient,
                                                       SummaryScriptFactory, answer, assistant, configured_thread,
                                                       context_events, result, run_turn, text, thinking)

KEY = ("thread-1", "outer")
OLDER = [{"turnId": "old", "messages": [{"role": "assistant", "content": "synthetic older completed work"}]}]


async def summarize_once(tmp_path, script, *, factory=None, configure=None):
    """One production summary attempt (no retry); returns its outcome, receipts and accounting."""
    factory = factory or SummaryScriptFactory([script])
    if configure is not None:
        configure(factory)
    instance = backend(tmp_path, factory, [])
    try:
        await configured_thread(instance, tmp_path)
        record = instance._record("thread-1")
        record["activeTurnId"] = "outer"  # the usage key of the turn that pays for the summary
        before = json.dumps(record["contextState"], sort_keys=True)
        try:
            with mock.patch.object(claude_context, "SUMMARY_ATTEMPTS", 1):
                summary, form, attempts = await instance._context_summary(record, {"effort": "max"}, OLDER, None)
            outcome = "accepted"
        except _CheckpointFailure as failure:
            summary, form, attempts, outcome = None, None, failure.attempts, failure.category
        assert json.dumps(record["contextState"], sort_keys=True) == before, "a summary never edits the context"
        return SimpleNamespace(outcome=outcome, summary=summary, form=form, attempts=attempts,
                               usage=instance._async_usage.get(KEY, {}), incomplete=KEY in instance._usage_incomplete,
                               summarizers=factory.summarizers(), prompts=list(factory.summary_prompts))
    finally:
        record = instance._record("thread-1")
        record.pop("activeTurnId", None)
        await instance.stop()


# --- Terminal boundary -------------------------------------------------------------


@pytest.mark.parametrize("stop_reason,terminal_reason,evidence", [
    ("end_turn", "completed", "stop_reason"),
    ("stop_sequence", "completed", "stop_reason"),  # complete upstream too; not a defect by itself
    (None, "completed", "terminal_reason"),  # the optional stop field is absent
    (None, None, "stream"),  # both optional fields absent: the stream evidence decides
], ids=["end_turn", "stop_sequence", "no-stop-reason", "no-optional-metadata"])
async def test_complete_terminal_shapes_are_accepted_by_the_production_path(
        tmp_path, stop_reason, terminal_reason, evidence):
    run = await summarize_once(tmp_path, answer(HANDOFF, stop_reason=stop_reason, terminal_reason=terminal_reason))
    assert run.outcome == "accepted" and (run.summary, run.form) == (HANDOFF, "plain_text")
    [attempt] = run.attempts
    assert attempt["completionEvidence"] == evidence
    assert run.usage["input_tokens"] == 100 and not run.incomplete  # terminal usage, exactly once
    [summarizer] = run.summarizers
    assert summarizer.interrupts == 0 and len(run.prompts) == 1


@pytest.mark.parametrize("stop_reason,category,continuation", [
    ("model_context_window_exceeded", "truncated", "context_window"),
    ("max_tokens", "truncated", "max_tokens"),
    ("unknown_stop_reason", "unexpected_stop", None),
    ("tool_deferred", "tool_use", None),
    ("pause_turn", "incomplete_response", "pause_turn"),
    ("refusal", "refused", None),
])
async def test_explicit_incomplete_or_unknown_result_stops_are_never_committed(
        tmp_path, stop_reason, category, continuation):
    # A useful-looking, complete-looking handoff does not make such a result complete.
    run = await summarize_once(tmp_path, answer(HANDOFF, stop_reason=stop_reason))
    assert run.outcome == category and run.summary is None
    [attempt] = run.attempts
    assert attempt["outcome"] == category and attempt.get("continuation") == continuation
    assert attempt["usageReported"] is True
    assert run.usage["input_tokens"] == 100 and not run.incomplete  # billed once, not lost


@pytest.mark.parametrize("stop_reason,category", [
    ("model_context_window_exceeded", "truncated"),
    ("novel_stop_reason", "unexpected_stop"),
])
async def test_explicit_incomplete_stream_stops_are_interrupted_and_accounted(tmp_path, stop_reason, category):
    run = await summarize_once(tmp_path, [assistant(thinking()), assistant(text(HANDOFF), stop_reason=stop_reason),
                                          result(HANDOFF)])
    assert run.outcome == category
    [summarizer] = run.summarizers
    assert summarizer.interrupts == 1  # nothing after the boundary is paid for or accepted
    assert run.usage["input_tokens"] == 1000 and not run.incomplete  # the aborted session's result, once


# --- Plain-text handoffs through the checkpoint commit -------------------------------


USEFUL_PLAIN_TEXTS = {
    "plain": "Goal: repair the summary parser. Done: added offline cases. Next: run the suite.",
    "newlines": "Goal: repair summaries.\nCompleted: parser tests.\nNext: lifecycle coverage.",
    "quotes-and-braces": 'Keep the option "strict" and literal braces {name}; no JSON envelope is needed.',
    "unicode": "Готово: проверены ошибки. Далее: тесты отмены. Unicode: λ, 日本語, café.",
    "quoted-refusal": ('The prior user quoted "I cannot help with that request" as a test fixture. '
                       "The parser fix is complete."),
    "bullets": "- Fixed the TTL bug in src/cache.py\n- Tests: 41 passed, 1 xfail\n* Next: update CHANGELOG.md",
    "headingless-paragraphs": ("The task is the ORB-4127 cache TTL change.\n\nWork so far: the parser was "
                               "rewritten; `pytest -q` passes.\n\nRemaining: document the new option."),
    "brace-first": "{ttl_seconds} must stay a template variable; the config loader now validates it.",
    "standalone-blocker": "I cannot run the integration test because the database is unavailable.",
    "apology-with-blocker": "I'm sorry, the integration test could not run because the database is unavailable.",
}


MARKDOWN_WRAPPERS = (
    ("", ""), ("**", "**"), ("__", "__"), ("*", "*"), ("## ", ""), ("# ", ""),
)


def terse_markdown_handoff(wrapper, punctuated):
    prefix, suffix = wrapper
    punctuation = "." if punctuated else ""
    return "\n".join(prefix + line + punctuation + suffix
                     for line in ("Parser tests pass", "Next: fix import resolution"))


@pytest.mark.parametrize("wrapper", MARKDOWN_WRAPPERS,
                         ids=["plain", "bold", "underscore-bold", "italic", "heading", "title"])
@pytest.mark.parametrize("punctuated", (False, True), ids=["terse", "punctuated"])
def test_useful_markdown_format_never_makes_a_complete_handoff_empty(wrapper, punctuated):
    handoff = terse_markdown_handoff(wrapper, punctuated)
    assert claude_context.inspect_handoff(handoff) == (handoff, "plain_text")
    assert claude_context.handoff_content(handoff) == (2, 0)
    stream = claude_context.SummaryStream()
    stream.observe(assistant(text(handoff)))
    assert claude_context.inspect_summary(result(handoff), stream) == (handoff, "plain_text")
    # Useful formatting does not turn an explicitly truncated reply into a completion.
    with pytest.raises(claude_context.SummaryRejected, match="truncated"):
        claude_context.inspect_summary(result(handoff, stop_reason="max_tokens"), stream)


@pytest.mark.parametrize("wrapper", MARKDOWN_WRAPPERS,
                         ids=["plain", "bold", "underscore-bold", "italic", "heading", "title"])
@pytest.mark.parametrize("punctuated", (False, True), ids=["terse", "punctuated"])
async def test_useful_markdown_format_commits_the_same_production_checkpoint(tmp_path, wrapper, punctuated):
    handoff = terse_markdown_handoff(wrapper, punctuated)
    factory = SummaryScriptFactory([answer(handoff)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        record = instance._record("thread-1")
        assert turn["status"] == "completed"
        assert record["contextState"]["generation"] == 1 and record["claudeSessionId"] != original
        assert record["contextState"]["summary"] == handoff  # original bytes, not normalized Markdown
        assert handoff in factory.main_prompts[-1][1]
        [compacted] = context_events(events)
        assert compacted["method"] == "thread/contextCompacted"
        assert [item["outcome"] for item in compacted["params"]["attempts"]] == ["accepted"]
        assert handoff not in json.dumps(compacted["params"])
        assert turn["usage"]["input_tokens"] == 7 + 7 + 100 + 7
    finally:
        await instance.stop()


@pytest.mark.parametrize("handoff", [
    "## Goals\n## Progress\n## Next steps",
    "# Handoff summary\n**Status:**\n**Next steps:**",
    "**Goals:** None\n**Progress:** N/A\n- Next steps: nothing yet",
], ids=["empty-headings", "empty-status-labels", "empty-markers"])
async def test_empty_markdown_templates_still_preserve_original_production_context(tmp_path, handoff):
    with pytest.raises(claude_context.SummaryRejected, match="empty_summary"):
        claude_context.inspect_handoff(handoff)
    factory = SummaryScriptFactory([answer(handoff)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        record = instance._record("thread-1")
        assert turn["status"] == "completed"  # original context continued after optional threshold failure
        assert record["contextState"]["generation"] == 0 and record["claudeSessionId"] == original
        assert record["contextState"]["summary"] == ""
        [failed] = context_events(events)
        assert failed["method"] == "thread/contextCompactionFailed"
        assert [item["outcome"] for item in failed["params"]["attempts"]] == ["empty_summary", "empty_summary"]
    finally:
        await instance.stop()


@pytest.mark.parametrize("name", list(USEFUL_PLAIN_TEXTS))
async def test_useful_plain_text_without_headings_commits_the_checkpoint_as_written(tmp_path, name):
    handoff = USEFUL_PLAIN_TEXTS[name]
    factory = SummaryScriptFactory([answer(handoff)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        record = instance._record("thread-1")
        assert record["claudeSessionId"] != original and record["contextState"]["generation"] == 1
        assert record["contextState"]["summary"] == handoff  # preserved exactly: no repair, no extraction
        assert handoff in factory.main_prompts[-1][1]
        [compacted] = context_events(events)
        assert compacted["method"] == "thread/contextCompacted"
        assert compacted["params"]["summaryFormat"] == "plain_text"
        assert [attempt["outcome"] for attempt in compacted["params"]["attempts"]] == ["accepted"]
        assert handoff not in json.dumps(compacted["params"], ensure_ascii=False)
    finally:
        await instance.stop()


async def test_refusal_only_output_padded_with_headings_never_replaces_the_context(tmp_path):
    factory = SummaryScriptFactory([answer(REFUSAL_PADDED_WITH_HEADINGS)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"  # threshold trigger: the original context continued
        record = instance._record("thread-1")
        assert record["claudeSessionId"] == original and record["contextState"]["generation"] == 0
        assert record["contextState"]["summary"] == ""
        [failed] = context_events(events)
        assert failed["method"] == "thread/contextCompactionFailed"
        attempts = failed["params"]["attempts"]
        assert [attempt["outcome"] for attempt in attempts] == ["refused", "refused"]
        assert all(attempt["contentRefusal"] is True for attempt in attempts)
        # Each refused attempt was a complete paid response: counted once each.
        assert turn["usage"]["input_tokens"] == 7 + 7 + 100 + 100 + 7
    finally:
        await instance.stop()


# --- Input admission ------------------------------------------------------------------


async def test_admission_reserves_the_whole_combined_cap_before_any_query(tmp_path):
    # 102,000 used of 160,000 cannot hold a 64,000-token combined
    # output/reasoning cap plus a margin: a smaller fixed reserve would send it.
    run = await summarize_once(tmp_path, answer(), configure=lambda factory: factory.summary_usage.update(
        totalTokens=102_000, maxTokens=160_000))
    assert run.outcome == "input_too_large" and run.prompts == [] and run.usage == {}
    [attempt] = run.attempts
    assert attempt["phase"] == "measure" and attempt["contextBaselineTokens"] == 102_000
    assert attempt["contextCapacityTokens"] == 160_000
    assert attempt["admissionRequiredTokens"] >= 102_000 + attempt["inputTokensEstimate"] + 64_000 + 8_192
    [summarizer] = run.summarizers
    # Nothing was silently changed to make it fit.
    assert summarizer.options.effort == "max"
    assert summarizer.options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


@pytest.mark.parametrize("slack,outcome", [(1, "accepted"), (0, "input_too_large")])
async def test_admission_boundary_is_exact_for_the_measured_baseline_and_estimate(tmp_path, slack, outcome):
    state = claude_context.new_context()  # the configured thread's state when the summary runs
    estimate = claude_context.estimate_tokens(claude_context.summary_prompt(state, OLDER))
    required = claude_context.summary_admission(5_000, estimate, 10**9)["requiredTokens"]
    run = await summarize_once(tmp_path, answer(), configure=lambda factory: factory.summary_usage.update(
        totalTokens=5_000, maxTokens=required + slack))
    assert run.outcome == outcome
    assert run.attempts[0]["admissionRequiredTokens"] == required
    assert len(run.prompts) == (1 if outcome == "accepted" else 0)


def test_admission_never_reports_a_character_estimate_as_a_token_guarantee():
    # The margin grows with the estimate because characters are not tokens.
    small = claude_context.summary_admission(0, 1_000, 10**9)
    large = claude_context.summary_admission(0, 400_000, 10**9)
    assert small["marginTokens"] == 8_192 + 250 and large["marginTokens"] == 8_192 + 100_000
    assert large["requiredTokens"] == 400_000 + 64_000 + 108_192


# --- Accounting: delivered terminal usage and failing interrupts ----------------------


class LateResultFailingInterruptClient(SummaryScriptClient):
    """The pending read receives the terminal result while the interrupt fails."""

    async def receive_response(self):
        if not self.summarizer:
            async for message in super().receive_response():
                yield message
            return
        while self.script:
            item = self.script.pop(0)
            if item is GATE:
                self.factory.waiting_read.set()  # a read is now pending on the stream
                await self.factory.release.wait()
                continue
            yield item
            if item is ABORTED:
                return

    async def interrupt(self):
        self.interrupts += 1
        if not self.summarizer:
            return
        self.interrupt_task = asyncio.current_task()
        self.factory.release.set()  # the CLI already wrote its terminal result
        for _ in range(20):
            await asyncio.sleep(0)
        raise CLIConnectionError("synthetic control failure after the result was delivered")


class LateResultFactory(SummaryScriptFactory):
    def __init__(self, scripts, **kwargs):
        super().__init__(scripts, **kwargs)
        self.waiting_read = asyncio.Event()

    def __call__(self, options):
        client = LateResultFailingInterruptClient(self, options)
        self.clients.append(client)
        return client


async def test_terminal_usage_delivered_during_a_failing_interrupt_is_counted_once(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_context, "SUMMARY_TIMEOUT_SECONDS", 0.3)
    factory = LateResultFactory([[assistant(text("partial handoff")), GATE, ABORTED]])
    run = await summarize_once(tmp_path, None, factory=factory)
    assert run.outcome == "timeout"
    [attempt] = run.attempts
    assert attempt["usageReported"] is True and "stopIncomplete" not in attempt
    assert run.usage["input_tokens"] == 1000 and not run.incomplete
    [summarizer] = run.summarizers
    assert summarizer.interrupts == 1 and summarizer.interrupt_task is summarizer.connect_task
    assert summarizer.disconnect_task is summarizer.connect_task  # same-task SDK ownership


async def test_interrupted_turn_keeps_terminal_usage_delivered_during_a_failing_interrupt(tmp_path):
    factory = LateResultFactory([[assistant(text("partial handoff")), GATE, ABORTED]])
    instance = backend(tmp_path, factory, events := [])
    try:
        await configured_thread(instance, tmp_path)
        original = instance._record("thread-1")["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Task."}]})
        async with asyncio.timeout(2):
            await factory.waiting_read.wait()  # the partial text was read; the next read is pending
        assert await asyncio.wait_for(
            instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}), 2) == {}
        turn = (await wait_completed(events))["params"]["turn"]
        assert turn["status"] == "interrupted" and "usageIncomplete" not in turn
        assert turn["usage"]["input_tokens"] == 7 + 7 + 1000  # coder slices and the summary session, once each
        assert context_events(events) == []  # cancellation is not a checkpoint failure
        assert instance._record("thread-1")["claudeSessionId"] == original
    finally:
        factory.release.set()
        await instance.stop()


async def test_missing_terminal_usage_with_a_failing_interrupt_stays_explicitly_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_context, "SUMMARY_TIMEOUT_SECONDS", 0.3)
    factory = SummaryScriptFactory([[assistant(text("partial handoff")), HANG]])
    factory.interrupt_error = CLIConnectionError("synthetic control failure")
    run = await summarize_once(tmp_path, None, factory=factory)
    assert run.outcome == "timeout"
    [attempt] = run.attempts
    assert attempt["stopIncomplete"] is True and "usageReported" not in attempt
    assert run.usage == {} and run.incomplete  # never an invented zero or estimate


# --- Fences and prose around a legacy envelope ------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Valid fenced JSON followed by prose. It opens as the legacy envelope
    # fence, so the legacy rules apply: not one whole fence.
    (FENCED + "\nDone.", ("invalid_json", "fence_unclosed")),
    # Two complete fenced JSON documents concatenated.
    (FENCED + "\n" + FENCED, ("invalid_json", "fence_ambiguous")),
    # Tilde-fenced JSON. A tilde fence is not one of the legacy envelope forms,
    # so it is complete plain text kept as written; nothing is decoded or
    # extracted from it.
    ("~~~json\n" + BARE + "\n~~~", ("plain_text", None)),
], ids=["fenced-json-then-prose", "two-fenced-json-documents", "tilde-fenced-json"])
def test_malformed_fences_and_prose_around_json_have_pinned_outcomes(raw, expected):
    category, label = expected
    if category == "plain_text":
        assert claude_context.inspect_summary(result(raw)) == (raw, "plain_text")
        return
    with pytest.raises(claude_context.SummaryRejected) as rejected:
        claude_context.inspect_summary(result(raw))
    assert rejected.value.category == category and rejected.value.details["formatError"] == label


async def test_legacy_fenced_json_checkpoint_keeps_session_tail_usage_and_privacy(tmp_path):
    """The plaintext checkpoint assertions, repeated for the legacy fenced-JSON format."""
    factory = SummaryScriptFactory([answer(FENCED)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        record = instance._record("thread-1")
        assert record["contextState"]["generation"] == 1 and record["claudeSessionId"] != original
        assert record["contextState"]["summary"] == GOOD["summary"]
        assert len(factory.summary_prompts) == 1
        bootstrap = factory.main_prompts[-1][1]
        assert GOOD["summary"] in bootstrap and "private-older-evidence-1" not in bootstrap
        assert "verbatim-result-2" in bootstrap and "Implement the original request." in bootstrap
        [compacted] = context_events(events)
        assert compacted["method"] == "thread/contextCompacted"
        params = compacted["params"]
        assert params["summaryFormat"] == "legacy_fenced_json" and params["trigger"] == "threshold"
        assert params["summaryChars"] == len(GOOD["summary"]) and params["summarizedRounds"] == 1
        [attempt] = params["attempts"]
        assert attempt["outcome"] == "accepted" and attempt["summaryFormat"] == "legacy_fenced_json"
        assert SECRET not in json.dumps(params) and "private-older-evidence" not in json.dumps(params)
        assert turn["usage"]["input_tokens"] == 7 + 7 + 100 + 7
    finally:
        await instance.stop()


def test_new_receipt_fields_survive_the_bounded_event_journal_without_content():
    from supervisor.controller_parts.event_protocol import _bounded_subagent_event_payload

    payload = _bounded_subagent_event_payload("thread/contextCompactionFailed", {"category": "refused", "attempts": [
        {"attempt": 1, "outcome": "refused", "contentRefusal": True, "completionEvidence": "stop_reason",
         "contextBaselineTokens": 5_000, "contextCapacityTokens": 200_000, "admissionRequiredTokens": 90_000,
         "text": SECRET}]})
    assert payload["attempts"] == [{"attempt": 1, "outcome": "refused", "content_refusal": True,
                                    "completion_evidence": "stop_reason", "context_baseline_tokens": 5_000,
                                    "context_capacity_tokens": 200_000, "admission_required_tokens": 90_000}]
    assert SECRET not in json.dumps(payload)


def test_bare_legacy_json_and_its_content_rules_still_apply():
    assert claude_context.inspect_summary(result(BARE)) == (GOOD["summary"], "legacy_json")
    for summary, category in (("None.", "empty_summary"), ("I'm sorry, I cannot summarize this.", "refused")):
        with pytest.raises(claude_context.SummaryRejected, match=category):
            claude_context.inspect_summary(result(json.dumps({"status": "complete", "summary": summary})))
