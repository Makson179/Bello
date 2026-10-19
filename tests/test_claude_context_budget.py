"""Claude summary deadline: one 500-second budget for the whole step, bounded cleanup, prompt cancellation.

All timing here is SYNTHETIC: a deterministic virtual clock (tests/support/
virtual_clock.py) drives the real backend code with scripted SDK fakes, so
deadlines fire at their exact virtual instant. A fake delayed summarizer
reproduces ordering only, never a real provider latency.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from supervisor.runtime import claude_context
from tests.support.claude_summary_timing import (Delay, Recorder, TimedScriptFactory, run_turn, start_thread,
                                                 timed_backend)
from tests.support.virtual_clock import VirtualClockLoop, run_virtual
from tests.test_runtime_claude_context_summary import BARE, HANDOFF, answer, assistant, result, text, thinking


def test_virtual_clock_fires_timers_at_their_exact_instant():
    async def scenario():
        loop = asyncio.get_running_loop()
        assert isinstance(loop, VirtualClockLoop)
        await asyncio.sleep(121)
        slept = loop.time()
        try:
            async with asyncio.timeout(500):
                await asyncio.sleep(10_000)
        except TimeoutError:
            fired = loop.time()
        return slept, fired

    assert run_virtual(scenario) == (121.0, 621.0)


def summary_turn(tmp_path, scripts, *, configure=None, during=None):
    """One outer turn whose checkpoint runs the scripted summarizer; returns the record."""

    async def scenario():
        factory = TimedScriptFactory(scripts)
        if configure is not None:
            configure(factory)
        recorder = Recorder()
        instance = timed_backend(tmp_path, factory, recorder)
        try:
            original = await start_thread(instance, tmp_path)
            helper = asyncio.create_task(during(instance, factory)) if during is not None else None
            turn = await run_turn(instance, recorder)
            if helper is not None:
                await helper
            return {"factory": factory, "recorder": recorder, "instance": instance, "original": original,
                    "turn": turn, "now": asyncio.get_running_loop().time()}
        finally:
            await instance.stop()

    return run_virtual(scenario)


def response_after(seconds, *, first_message_first=True):
    if first_message_first:
        return [assistant(thinking()), Delay(seconds), assistant(text(HANDOFF)), result(HANDOFF)]
    return [Delay(seconds), assistant(thinking()), assistant(text(HANDOFF)), result(HANDOFF)]


def rejected_after(seconds):
    """A complete response that is rejected (an unclosed JSON fence) after ``seconds``."""
    return [Delay(seconds), *answer("```json\n" + BARE)]


@pytest.mark.parametrize("seconds", [121.0, 300.0, 499.5, 499.999])
def test_a_slow_summary_finishing_within_the_deadline_is_accepted(tmp_path, seconds):
    run = summary_turn(tmp_path, [response_after(seconds)])
    assert run["turn"]["status"] == "completed"
    record = run["instance"]._record("thread-1")
    assert record["contextState"]["generation"] == 1 and record["contextState"]["summary"] == HANDOFF
    [compacted] = run["recorder"].context()
    [attempt] = compacted["params"]["attempts"]
    assert attempt["outcome"] == "accepted" and attempt["responseMs"] == int(seconds * 1000)
    assert run["factory"].interrupted_at == []  # nothing left to stop


def test_a_shorter_configured_deadline_is_honored(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_context, "SUMMARY_TIMEOUT_SECONDS", 120)
    run = summary_turn(tmp_path, [response_after(121.0)])
    [failed] = run["recorder"].context()
    assert [attempt["outcome"] for attempt in failed["params"]["attempts"]] == ["timeout"]
    assert failed["params"]["summaryTimeoutSeconds"] == 120


@pytest.mark.parametrize("first_message_first", [True, False])
def test_the_deadline_is_exactly_500_seconds_and_a_spent_deadline_is_not_retried(tmp_path, first_message_first):
    run = summary_turn(tmp_path, [response_after(500.001, first_message_first=first_message_first)])
    factory, recorder = run["factory"], run["recorder"]
    assert run["turn"]["status"] == "completed"  # threshold trigger: the original context continues
    record = run["instance"]._record("thread-1")
    assert record["claudeSessionId"] == run["original"] and record["contextState"]["generation"] == 0
    [failed] = recorder.context()
    params = failed["params"]
    attempts = params["attempts"]
    # Interrupted at exactly +500.000 s (connect and measure take no virtual
    # time here). The deadline is spent, so there is no second attempt.
    assert [attempt["outcome"] for attempt in attempts] == ["timeout"]
    assert [stop - start for start, stop in zip(factory.queried_at, factory.interrupted_at)] == [500.0]
    assert [attempt["durationMs"] for attempt in attempts] == [500_000]
    phase = "streaming" if first_message_first else "await_first_message"
    assert [attempt["phase"] for attempt in attempts] == [phase]
    assert all(attempt["usageReported"] for attempt in attempts)
    assert len(factory.summary_prompts) == 1
    assert params["summaryTimeoutSeconds"] == 500 and params["category"] == "timeout"
    assert run["turn"]["usage"]["input_tokens"] == 7 * 3 + 1000  # the stopped session, once


def test_a_retry_runs_only_in_the_time_the_first_attempt_left(tmp_path):
    # The first attempt is rejected after 100 s; the second may use the remaining
    # 400 s, not a fresh 500 s, so a 450 s response is cut off at exactly 500 s.
    run = summary_turn(tmp_path, [rejected_after(100.0), response_after(450.0)])
    factory = run["factory"]
    [failed] = run["recorder"].context()
    attempts = failed["params"]["attempts"]
    assert [attempt["outcome"] for attempt in attempts] == ["invalid_json", "timeout"]
    assert attempts[1]["retryNote"] is True
    assert [attempt["durationMs"] for attempt in attempts] == [100_000, 400_000]
    assert factory.queried_at == [0.0, 100.0] and factory.interrupted_at == [500.0]
    assert run["now"] == pytest.approx(500.0, abs=1e-6)  # the whole step, retry included
    assert run["instance"]._record("thread-1")["contextState"]["generation"] == 0


def test_a_retry_that_fits_the_remaining_time_commits(tmp_path):
    run = summary_turn(tmp_path, [rejected_after(100.0), response_after(399.0)])
    assert run["turn"]["status"] == "completed"
    [compacted] = run["recorder"].context()
    assert [attempt["outcome"] for attempt in compacted["params"]["attempts"]] == ["invalid_json", "accepted"]
    assert run["instance"]._record("thread-1")["contextState"]["generation"] == 1
    assert run["factory"].interrupted_at == []
    assert run["now"] == pytest.approx(499.0, abs=1e-6)


def test_a_rejection_just_before_the_deadline_leaves_almost_no_time_for_the_retry(tmp_path):
    run = summary_turn(tmp_path, [rejected_after(499.5), response_after(10.0)])
    [failed] = run["recorder"].context()
    attempts = failed["params"]["attempts"]
    assert [attempt["outcome"] for attempt in attempts] == ["invalid_json", "timeout"]
    assert attempts[1]["durationMs"] == 500
    assert run["now"] == pytest.approx(500.0, abs=1e-6)


@pytest.mark.parametrize("phase", ["connect", "measure"])
def test_the_deadline_also_bounds_summarizer_startup_and_reports_the_phase(tmp_path, phase):
    def configure(factory):
        if phase == "connect":
            factory.summary_connect_gate = asyncio.Event()  # never released
        else:
            factory.usage_gate = asyncio.Event()

    run = summary_turn(tmp_path, [response_after(1.0)], configure=configure)
    [failed] = run["recorder"].context()
    attempts = failed["params"]["attempts"]
    assert [attempt["outcome"] for attempt in attempts] == ["timeout"]
    assert [attempt["phase"] for attempt in attempts] == [phase]
    assert run["factory"].summary_prompts == [], "no query was sent, so nothing needed an interrupt"
    assert run["factory"].interrupted_at == []
    assert [attempt["durationMs"] for attempt in attempts] == [500_000]


@pytest.mark.parametrize("terminal", ["aborted_result", "never"])
def test_cancellation_is_prompt_despite_the_500_second_deadline(tmp_path, terminal):
    marks = {}

    def configure(factory):
        factory.after_interrupt = [] if terminal == "never" else factory.after_interrupt

    async def during(instance, factory):
        loop = asyncio.get_running_loop()
        while not factory.queried_at:
            await asyncio.sleep(0.5)
        await asyncio.sleep(200.0)  # 200 s into a summary that would take 400 s
        marks["interrupt"] = loop.time()
        assert await instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}) == {}
        marks["acknowledged"] = loop.time()

    run = summary_turn(tmp_path, [response_after(400.0)], configure=configure, during=during)
    factory, recorder = run["factory"], run["recorder"]
    assert run["turn"]["status"] == "interrupted"
    # A terminal result after the interrupt ends cleanup at once; a missing one
    # costs exactly the 5-second stop bound, never the summary deadline.
    expected = 0.0 if terminal == "aborted_result" else claude_context.SUMMARY_STOP_TIMEOUT_SECONDS
    assert recorder.completed_at - marks["interrupt"] == pytest.approx(expected, abs=1e-6)
    assert marks["acknowledged"] - marks["interrupt"] == pytest.approx(expected, abs=1e-6)
    assert factory.interrupted_at == [marks["interrupt"]]
    assert len(factory.summary_prompts) == 1, "no summary call after cancellation"
    assert recorder.context() == []
    record = run["instance"]._record("thread-1")
    assert record["claudeSessionId"] == run["original"] and record["contextState"]["generation"] == 0
    assert record["contextState"]["failedCheckpoints"] == 0, "cancellation is not a checkpoint failure"
    if terminal == "never":
        assert run["turn"]["usageIncomplete"] is True and run["turn"]["usage"]["input_tokens"] == 7 + 7
    else:
        assert "usageIncomplete" not in run["turn"] and run["turn"]["usage"]["input_tokens"] == 7 + 7 + 1000


def test_budget_values_reach_every_receipt_and_the_cli_contract(tmp_path):
    run = summary_turn(tmp_path, [response_after(1.0)])
    [compacted] = run["recorder"].context()
    assert compacted["params"]["summaryTimeoutSeconds"] == 500
    assert compacted["params"]["summaryMaxOutputTokens"] == 64_000
    [summarizer] = run["factory"].summarizers()
    assert summarizer.options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert json.dumps(compacted["params"]).count("Goals") == 0
