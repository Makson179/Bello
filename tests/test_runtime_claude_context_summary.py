"""Claude no-need-for-context summaries: contract, lifecycle and accounting (offline).

Summarizer fixtures are CLI-shaped stream-json frames parsed by the pinned
SDK's own message parser, so they follow the real text-result path. The shapes
mirror what the managed CLI delivers (scripts/verify_claude_context.py): each
content block arrives as its own AssistantMessage sharing one message id,
``ResultMessage.result`` repeats only the LAST text block, Markdown fences are
kept verbatim, and a truncated, thinking-only or safety-stopped answer is
continued by the CLI itself, with an injected user turn, a second response id
and a ``num_turns=2`` success result holding only the last fragment.

All fixtures in this module are synthetic: no CLI process, network, credential
or model request is involved.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import mmap

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from supervisor.controller_parts.event_protocol import _bounded_subagent_event_payload
from supervisor.runtime import claude_context
from supervisor.runtime.claude_context import (SummaryRejected, SummaryStream, inspect_handoff, inspect_summary,
                                               summary_payload)
from tests.test_runtime_claude import FakeClient, FakeFactory, backend, result_message, wait_completed


SESSION = "22222222-2222-4222-8222-222222222222"
SECRET = "PRIVATE-SUMMARY-CONTENT"
HANDOFF = (
    "## Goals\nFinish the parser repair for " + SECRET + ".\n\n"
    "## Constraints\nKeep the public API unchanged.\n\n"
    "## Progress\nThe earlier inspection completed; tests/test_parser.py passes.\n\n"
    "## Decisions\nKeep the streaming decoder.\n\n"
    "## Next steps\n1. Run the full suite.\n\n"
    "## Critical data\nCommand: pytest -q tests/test_parser.py"
)
GOOD = {"status": "complete", "summary": f"{SECRET}: the earlier inspection completed."}
BARE = json.dumps(GOOD)
FENCED = "```json\n" + json.dumps(GOOD, indent=2) + "\n```"
OUTPUT_LIMIT_NOTICE = ("Output token limit hit. Resume directly — no apology, no recap of what you were "
                       "doing. Pick up mid-thought if that is where the cut happened. Break remaining work "
                       "into smaller pieces.")
OUTPUT_LIMIT_CUT_NOTICE = ("Your response was cut off because it exceeded the output token limit. Continue "
                           "from where you left off.")
STREAM_CUT_NOTICE = ("Your response above was cut off mid-stream. Resume directly from where it stops — no "
                     "apology, no recap. If none of it survived, answer the request from the start.")
NO_OUTPUT_NOTICE = ("[Your previous response had no visible output. Please continue and produce a "
                    "user-visible response.]")
SAFETY_NOTICE = ("Your response above was stopped by a safety classifier — this is not a tool or API "
                 "error. The rest of it was withheld.")


def text(value):
    return {"type": "text", "text": value}


def thinking(value="private reasoning"):
    return {"type": "thinking", "thinking": value, "signature": "c2ln"}


def assistant(*blocks, message_id="msg_summary_1", stop_reason=None, error=None, model="claude-sonnet-5-5"):
    frame = {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None, "uuid": "a-" + message_id,
             "message": {"id": message_id, "type": "message", "role": "assistant", "model": model,
                         "content": list(blocks), "stop_reason": stop_reason,
                         # Per-response usage must never be added to the turn total.
                         "usage": {"input_tokens": 999_999, "output_tokens": 999_999}}}
    if error is not None:
        frame["error"] = error
    return parse_message(frame)


def injected(notice):
    """A CLI-authored meta turn, as the pinned CLI streams it to the SDK."""
    return parse_message({"type": "user", "session_id": SESSION, "parent_tool_use_id": None, "uuid": "u-1",
                          "message": {"role": "user", "content": [text(notice)]}})


def result(value=HANDOFF, *, stop_reason="end_turn", subtype="success", is_error=False, num_turns=1,
           terminal_reason="completed", api_error_status=None, structured_output=None, input_tokens=100):
    frame = {"type": "result", "subtype": subtype, "duration_ms": 1500, "duration_api_ms": 1400,
             "is_error": is_error, "num_turns": num_turns, "session_id": SESSION, "stop_reason": stop_reason,
             "total_cost_usd": 0.25, "usage": {"input_tokens": input_tokens, "output_tokens": 20},
             "result": value, "terminal_reason": terminal_reason, "uuid": "r-1"}
    if structured_output is not None:
        frame["structured_output"] = structured_output
    if api_error_status is not None:
        frame["api_error_status"] = api_error_status
    return parse_message(frame)


def answer(value=HANDOFF, **kwargs):
    """One complete summarizer response: thinking, then one visible text block."""
    return [assistant(thinking()), assistant(text(value)), result(value, **kwargs)]


def blocks_answer(*parts, **kwargs):
    """One response whose visible text spans several text blocks (the CLI's
    result repeats only the last one)."""
    messages = []
    for index, part in enumerate(parts):
        if index:
            messages.append(assistant(thinking("between blocks")))
        messages.append(assistant(text(part)))
    return [*messages, result(parts[-1], **kwargs)]


CONTINUED = [  # output-limit recovery: the result holds a complete-looking fragment only
    assistant(thinking()),
    assistant(text("## Goals\nFinish the repair of " + SECRET + " first half")),
    injected(OUTPUT_LIMIT_NOTICE),
    assistant(text(HANDOFF), message_id="msg_summary_2"),
    result(HANDOFF, num_turns=2, input_tokens=1000),
]
HANG = object()
GATE = object()  # the stream pauses until factory.release is set, then continues
# The CLI ends an interrupted query with one aborted result for the session.
ABORTED = result(None, subtype="error_during_execution", is_error=True, terminal_reason="aborted_streaming",
                 stop_reason=None, input_tokens=1000)


def streamed(messages):
    """Observe a scripted response as the backend does; return (stream, result)."""
    stream = SummaryStream()
    final = None
    for message in messages:
        if isinstance(message, ResultMessage):
            stream.observe_result(message)
            final = message
        else:
            stream.observe(message)
    return stream, final


# --- Plain-text handoff contract ----------------------------------------------


JSON_HOSTILE = (
    "## Goals\nRepair the \"quoted\" config loader; keep C:\\temp\\out and {braces} intact.\n\n"
    "## Constraints\n- Never touch /etc/hosts\n- Tabs\tstay\ttabs\n\n"
    "## Progress\nRan:\n```bash\npytest -q -k 'not slow'\n```\nResult: 3 failed (\"KeyError: 'x'\").\n\n"
    "## Decisions\nUse json.loads(strict=False)? No: rejected.\n\n"
    "## Next steps\n1. Fix parse_line() in src/loader.py:88\n2. Re-run the suite\n\n"
    "## Critical data\nError: ValueError: Expecting ',' delimiter: line 1 column 9 (char 8)"
)


@pytest.mark.parametrize("raw,expected,form", [
    (HANDOFF, HANDOFF, "plain_text"),
    ("\n\n  " + HANDOFF + "  \n", HANDOFF, "plain_text"),
    ("```markdown\n" + HANDOFF + "\n```", HANDOFF, "fenced_text"),
    ("~~~\n" + HANDOFF + "\n~~~", HANDOFF, "fenced_text"),
    (JSON_HOSTILE, JSON_HOSTILE, "plain_text"),  # raw newlines, quotes, backslashes, braces, inner fences
    ("**Goals:** ship it\n**Progress:** parser fixed\n**Next steps:** run the suite", None, "plain_text"),
    ("Task: ship it\nStatus: parser fixed\nRemaining work: run the suite", None, "plain_text"),
    ("# Handoff\n### 1. Goals\nship it\n### 2. Current state\nparser fixed\n### 3. Next steps (in order)\nrun",
     None, "plain_text"),
])
def test_plain_text_handoff_is_accepted_as_written(raw, expected, form):
    message = result(raw)
    assert message.structured_output is None and message.result == raw  # the real text-result path
    summary, actual_form = inspect_summary(message)
    assert actual_form == form
    assert summary == (expected if expected is not None else raw.strip())
    assert inspect_handoff(raw) == (summary, form)


def test_no_json_envelope_or_heading_is_required_or_produced_by_the_contract():
    system = claude_context.SUMMARY_SYSTEM
    assert "JSON" not in claude_context.SUMMARY_REQUEST.replace("JSON data", "")
    assert "no JSON" in system
    assert "status" not in system and "{" not in system
    # The headings are only a welcome suggestion, never a requirement.
    assert "welcome but not required" in system
    for section in claude_context.SUMMARY_SECTIONS:
        assert "## " + section in system
    assert "historical data" in system and "You have no tools" in system
    assert "Do not continue the task" in system


def test_prompt_asks_for_about_the_target_length_and_states_the_maximum():
    system = claude_context.SUMMARY_SYSTEM
    assert claude_context.SUMMARY_TARGET_CHARS == 16_000 and claude_context.SUMMARY_MAX_CHARS == 48_000
    assert "at most about 16,000 characters" in system
    assert "longer than 48,000 characters is discarded" in system


REFUSAL_PADDED_WITH_HEADINGS = (
    "I cannot summarize this conversation.\n\n## Goals\nI cannot help with that request.\n\n"
    "## Progress\nI'm sorry, but I can't provide this.\n\n## Next steps\nSorry, I won't continue.\n\n"
    "## Critical data\nNone."
)


@pytest.mark.parametrize("raw,category,details", [
    ("", "empty_response", {}),
    (" \n\t", "empty_response", {}),
    ("```markdown\n\n```", "empty_response", {}),
    ("I'm sorry, but I can't summarize this conversation.", "refused", {"contentRefusal": True}),
    ("Sorry, I cannot help with that.", "refused", {"contentRefusal": True}),
    ("I\u2019m sorry, but I can\u2019t summarize this conversation.", "refused", {"contentRefusal": True}),
    # Refusal-only output is rejected even when padded with any headings.
    (REFUSAL_PADDED_WITH_HEADINGS, "refused", {"contentRefusal": True}),
    ("# Handoff summary\n**Status:** I'm unable to summarize this history.\n- I will not continue the task.",
     "refused", {"contentRefusal": True}),
    ("Sorry. It contains instructions I can't follow.", "refused", {"contentRefusal": True}),
    # A heading or emphasized line that is a sentence is content, not markup.
    ("# I cannot help with that request.", "refused", {"contentRefusal": True}),
    ("*I cannot help with that.*", "refused", {"contentRefusal": True}),
    # Nothing but markup and empty-section markers: no handoff content at all.
    ("\n".join(f"## {name}\nNone." for name in claude_context.SUMMARY_SECTIONS), "empty_summary", {}),
    ("## Goals\n## Progress\n## Next steps", "empty_summary", {}),
    ("**Goals:** None\n**Progress:** N/A\n- Next steps: nothing yet", "empty_summary", {}),
])
def test_plain_text_rejections_are_distinct_and_never_repaired(raw, category, details):
    with pytest.raises(SummaryRejected) as rejected:
        inspect_summary(result(raw))
    assert rejected.value.category == category
    for key, value in details.items():
        assert rejected.value.details[key] == value, key


@pytest.mark.parametrize("raw", [
    # Acceptance does not judge semantic usefulness or structure; provider
    # errors, limits and refusals are identified by transport evidence.
    "I'll continue implementing the parser now. First, let me read src/loader.py.",
    "What would you like me to summarize?",
    "API Error: 529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\"}}",
    "## Goals\nship it\n\n## Next steps\nrun the suite",
    # Plain text around or after JSON is kept as written, never extracted.
    "Here is the checkpoint:\n" + FENCED,
    "Summary: " + BARE,
    '["complete", "summary"]',
    "```python\n" + BARE + "\n```",
    "{name} is the template placeholder; keep it unescaped in src/render.py.",
    "```\n{name} stays a literal placeholder.\n```",
    # Content written only as emphasized or heading sentences is still content.
    "*The parser fix is complete; next run the suite.*",
    "## The parser fix landed in src/parser.py and the suite passes",
])
def test_text_without_the_suggested_structure_is_accepted_as_written(raw):
    summary, form = inspect_summary(result(raw))
    unfenced = raw.startswith("```\n")
    assert form == ("fenced_text" if unfenced else "plain_text")
    assert summary == (raw.strip()[4:-4].strip() if unfenced else raw.strip())


@pytest.mark.parametrize("raw", [
    # A useful handoff may quote or describe an earlier refusal as historical data.
    'The prior user quoted "I cannot help with that request" as a test fixture. The parser fix is complete.',
    "## Progress\nThe reviewer replied \u201cI\u2019m sorry, I can\u2019t approve this\u201d; the patch was revised.",
    "> I cannot help with that request.\nThat earlier refusal was a fixture; tests/test_refusal.py now passes.",
    "I cannot verify the CI result from this history. The parser fix landed in src/parser.py; next run pytest.",
    "Goals: keep the `I can't` message in errors.py unchanged.",
])
def test_quoted_or_described_refusals_do_not_reject_a_useful_handoff(raw):
    assert inspect_summary(result(raw)) == (raw.strip(), "plain_text")


def test_size_limits_cover_plain_text_and_raw_replies():
    body = HANDOFF + "\n" + "x" * (claude_context.SUMMARY_MAX_CHARS - len(HANDOFF) - 1)
    assert len(body) == claude_context.SUMMARY_MAX_CHARS
    assert inspect_handoff(body) == (body, "plain_text")
    with pytest.raises(SummaryRejected, match="oversized_summary") as rejected:
        inspect_handoff(body + "y")
    assert rejected.value.details == {"textChars": claude_context.SUMMARY_MAX_CHARS + 1}
    with pytest.raises(SummaryRejected, match="oversized_summary"):
        inspect_handoff(" " * (8 * claude_context.SUMMARY_MAX_CHARS + 1))


def test_lone_surrogates_are_replaced_so_the_state_file_stays_writable():
    summary, _form = inspect_handoff(HANDOFF + "\nmarker \ud800 end")
    assert "\ud800" not in summary and "marker \ufffd end" in summary
    summary.encode("utf-8")
    legacy, form = inspect_handoff('{"status":"complete","summary":"a \\ud800 b"}')
    assert legacy == "a \ufffd b" and form == "legacy_json"


# --- Legacy JSON envelope compatibility -----------------------------------------


@pytest.mark.parametrize("raw,form", [
    (BARE, "legacy_json"),
    ("\n  " + json.dumps(GOOD, indent=2) + "  \n", "legacy_json"),
    (FENCED, "legacy_fenced_json"),
    ("```\n" + BARE + "\n```", "legacy_fenced_json"),
    ("```JSON \n" + BARE + "\n```", "legacy_fenced_json"),
    ("````json\n" + BARE + "\n`````", "legacy_fenced_json"),
    ("```json\r\n" + BARE + "\r\n```\r\n", "legacy_fenced_json"),
])
def test_legacy_json_envelope_remains_accepted(raw, form):
    assert inspect_summary(result(raw)) == (GOOD["summary"], form)
    assert claude_context.checked_summary(result(raw)) == GOOD["summary"]


@pytest.mark.parametrize("raw,expected", [
    # The historical failure classes: a complete envelope whose only defect is
    # escaping. Only valid JSON escapes are decoded; everything else is literal.
    ('{"status":"complete","summary":"line one\nline two"}', "line one\nline two"),
    ('{"status":"complete","summary":"tab\there"}', "tab\there"),
    ('{"status":"complete","summary":"he said "hi" there"}', 'he said "hi" there'),
    ('{"status": "complete", "summary": "("" < "b")\nkeep \\"escaped\\" too"}', '("" < "b")\nkeep "escaped" too'),
    ('{"status":"complete","summary":"path C:\\\\temp and \\u00e9 and \\ud83d\\ude00\nraw"}',
     "path C:\\temp and \u00e9 and \U0001F600\nraw"),
    ('{"status":"complete","summary":"lone \\udc00 and bad \\x escape\n"}', "lone \ufffd and bad \\x escape"),
    ("```json\n{\"status\":\"complete\",\"summary\":\"fenced\nraw \"quote\"\"}\n```", 'fenced\nraw "quote"'),
    # Quoted JSON inside the summary cannot end it; an extra trailing member can.
    ('{"status":"complete","summary":"returns `{"a":"b","c":"d"}` here"}', 'returns `{"a":"b","c":"d"}` here'),
    ('{"status":"complete","summary":"he said "hi"","note":"x"}', 'he said "hi"'),
    ('{"status":"complete","summary":"done\nok","generated_at":"2026-10-01T00:00:00Z","n":3}', "done\nok"),
])
def test_legacy_envelope_escaping_edge_cases_are_recovered_exactly(raw, expected):
    summary, form = inspect_handoff(raw)
    assert summary == expected.strip()
    assert form.endswith("_recovered")


def test_legacy_recovery_work_is_bounded_on_pathological_replies():
    import time

    members = ',"k":"v"' * 6_000  # every quote looks like a possible end of the summary
    started = time.monotonic()
    with pytest.raises(SummaryRejected, match="invalid_json"):
        inspect_handoff('{"status":"complete","summary":"x\n' + members + " trailing garbage")
    assert time.monotonic() - started < 2.0
    # A long summary still ends at its real closing quote near the end.
    long_summary = "quoted \"x\", \"y\": z " * 2_000
    summary, form = inspect_handoff('{"status":"complete","summary":"' + long_summary + '"}')
    assert summary == long_summary.strip() and form == "legacy_json_recovered"


@pytest.mark.parametrize("raw,label", [
    ("```json\n" + BARE, "fence_unclosed"),  # unclosed: truncated
    ("````json\n" + BARE + "\n```", "fence_unclosed"),  # closing fence too short
    ("```json\n{\n```\n}\n```", "fence_ambiguous"),  # interior fence line is ambiguous
    ("```json\n[" + BARE + "]\n```", "not_json"),
    (BARE[:-12], "unterminated_string"),  # truncated bare JSON
    ('{"status":"complete","summary":"cut off mid', "unterminated_string"),
    ('{"status":"complete","summary":"a","summary":"b"}', "duplicate_key"),
    ('{"status":"complete","summary":"a","score":NaN}', "non_finite_number"),
    (BARE + "\nDone.", "extra_data"),
    # A tail that repeats the summary key is ambiguous, never guessed.
    ('{"status":"complete","summary":"he said "hi"","summary":"x"}', "expecting_delimiter"),
    ('{"status":"complete","summary":"a\nb" ', "raw_control_character"),  # and never closed
    ('{"status":"complete","summary":"a\nb"} trailing words', "raw_control_character"),
    ('{"summary":"a\nb","status":"complete"}', "raw_control_character"),  # not the legacy head
])
def test_malformed_legacy_json_is_rejected_with_location_and_reason(raw, label):
    with pytest.raises(SummaryRejected) as rejected:
        inspect_summary(result(raw))
    assert rejected.value.category == "invalid_json"
    details = rejected.value.details
    assert details["formatError"] == label
    if "formatErrorAt" in details:
        assert 0 <= details["formatErrorAt"] <= details["textChars"]
        assert 0 <= details["formatErrorAtPercent"] <= 100
    assert SECRET not in json.dumps(details)


@pytest.mark.parametrize("value,category", [
    ({"status": "refused", "summary": ""}, "refused"),
    ({"status": "done", "summary": "x"}, "invalid_shape"),
    ({"status": "complete"}, "invalid_shape"),
    ({"status": "complete", "summary": ["x"]}, "invalid_shape"),
    ({"status": "complete", "summary": " \n"}, "empty_summary"),
    ({"status": "complete", "summary": "x" * (claude_context.SUMMARY_MAX_CHARS + 1)}, "oversized_summary"),
    ({"status": "complete", "summary": "I'm sorry, I cannot summarize this history."}, "refused"),
])
def test_legacy_envelope_content_rejections_are_distinct(value, category):
    for message in (result(json.dumps(value)), result("```json\n" + json.dumps(value) + "\n```")):
        with pytest.raises(SummaryRejected) as rejected:
            inspect_summary(message)
        assert rejected.value.category == category


def test_limits_and_structured_output_keep_existing_validation():
    largest = {"status": "complete", "summary": "x" * claude_context.SUMMARY_MAX_CHARS}
    assert inspect_summary(result(json.dumps(largest)))[0] == largest["summary"]
    assert inspect_summary(result(None, structured_output=GOOD)) == (GOOD["summary"], "structured_output")
    with pytest.raises(SummaryRejected, match="refused"):
        inspect_summary(result(None, structured_output={"status": "refused", "summary": ""}))
    with pytest.raises(SummaryRejected, match="oversized_summary"):
        summary_payload(" " * (8 * claude_context.SUMMARY_MAX_CHARS + 1))
    with pytest.raises(SummaryRejected, match="empty_response"):
        inspect_summary(result(None))


@pytest.mark.parametrize("kwargs,category", [
    ({"stop_reason": "max_tokens"}, "truncated"),
    ({"stop_reason": "refusal"}, "refused"),
    ({"stop_reason": "tool_use"}, "tool_use"),
    ({"stop_reason": "pause_turn"}, "incomplete_response"),
    ({"num_turns": 2}, "incomplete_response"),  # a CLI continuation's last fragment
    ({"subtype": "error_max_turns", "is_error": True, "terminal_reason": "max_turns"}, "max_turns"),
    ({"is_error": True, "api_error_status": 529, "terminal_reason": "api_error"}, "provider_error"),
    ({"subtype": "error_during_execution", "is_error": True}, "result_error"),
    ({"terminal_reason": "aborted_streaming"}, "result_error"),
])
def test_result_level_failures_reject_even_a_complete_looking_handoff(kwargs, category):
    with pytest.raises(SummaryRejected) as rejected:
        inspect_summary(result(HANDOFF, **kwargs))
    assert rejected.value.category == category
    if category in {"truncated", "incomplete_response"}:
        assert rejected.value.details["continuation"] in {"max_tokens", "pause_turn", "num_turns"}


# --- One response: stream reconstruction and continuation detection ------------


def test_whole_answer_is_every_text_block_of_the_single_response():
    first, second = HANDOFF.split("## Critical data")
    stream, final = streamed(blocks_answer(first.rstrip("\n"), "## Critical data" + second))
    assert final.result == "## Critical data" + second  # the CLI repeats only the last block
    # That last block alone is valid text, but only a fragment of the handoff:
    # taking it would silently drop every earlier block.
    assert inspect_handoff(final.result)[0] == final.result != HANDOFF
    summary, form = inspect_summary(final, stream)
    assert form == "plain_text"
    assert summary == first.rstrip("\n") + "\n\n## Critical data" + second == HANDOFF
    receipt = stream.summary()
    assert receipt["responses"] == 1 and receipt["textBlocks"] == 2 and receipt["thinkingBlocks"] == 1


@pytest.mark.parametrize("messages", [
    [assistant(text(HANDOFF)), result("## Next steps\nsomething else")],  # result disagrees with the stream
    [result(HANDOFF)],  # a result without any streamed text block
])
def test_result_that_disagrees_with_the_stream_is_never_accepted(messages):
    stream, final = streamed(messages)
    with pytest.raises(SummaryRejected) as rejected:
        inspect_summary(final, stream)
    assert rejected.value.category == "incomplete_response"
    assert rejected.value.details == {"resultMismatch": True}


def test_stream_accepts_one_multi_block_response_and_rejects_continuations():
    stream = SummaryStream()
    stream.observe(parse_message({"type": "system", "subtype": "init", "session_id": SESSION}))
    stream.observe(assistant(thinking()))
    stream.observe(assistant(text(HANDOFF)))  # same response id: thinking + text blocks
    assert stream.summary() == {"responses": 1, "assistantMessages": 2, "injectedInputs": 0, "textBlocks": 1,
                                "textChars": len(HANDOFF), "thinkingBlocks": 1, "model": "claude-sonnet-5-5"}
    with pytest.raises(SummaryRejected, match="incomplete_response") as rejected:
        stream.observe(assistant(text(HANDOFF), message_id="msg_summary_2"))
    assert rejected.value.details == {"continuation": "second_response"}
    assert stream.summary()["continuation"] == "second_response"


@pytest.mark.parametrize("message,category,details", [
    (injected(OUTPUT_LIMIT_NOTICE), "truncated", {"continuation": "output_limit"}),
    (injected(OUTPUT_LIMIT_CUT_NOTICE), "truncated", {"continuation": "output_limit"}),
    (injected(STREAM_CUT_NOTICE), "incomplete_response", {"continuation": "stream_cut"}),
    (injected(NO_OUTPUT_NOTICE), "incomplete_response", {"continuation": "no_visible_output"}),
    (injected(SAFETY_NOTICE), "refused", {"continuation": "safety_stop"}),
    (injected("Any other CLI-injected turn"), "incomplete_response", {"continuation": "injected_input"}),
    (assistant(text("partial"), stop_reason="max_tokens"), "truncated", {"continuation": "max_tokens"}),
    (assistant(text(""), stop_reason="refusal", error="invalid_request"), "refused", {}),
    (assistant(text("API Error"), error="rate_limit"), "provider_error", {"assistantError": "rate_limit"}),
    (assistant(text("x"), error="novel_error"), "provider_error", {"assistantError": "other"}),
    (assistant(text("API Error: Claude's response exceeded the output token maximum."), model="<synthetic>"),
     "provider_error", {"syntheticMessage": True}),
    (assistant({"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": SECRET}}), "tool_use", {}),
])
def test_stream_classifies_injected_turns_tools_and_provider_errors(message, category, details):
    stream = SummaryStream()
    with pytest.raises(SummaryRejected) as rejected:
        stream.observe(message)
    assert rejected.value.category == category
    assert rejected.value.details == details


def test_attempt_receipt_fields_are_content_free():
    stream, message = streamed([assistant(thinking(SECRET)), assistant(text(HANDOFF)),
                                result(HANDOFF, api_error_status=200)])
    receipt = stream.summary()
    assert receipt == {"resultSubtype": "success", "stopReason": "end_turn", "terminalReason": "completed",
                       "numTurns": 1, "apiErrorStatus": 200, "resultChars": len(HANDOFF),
                       "structuredOutput": False, "outputTokens": 20, "responses": 1, "assistantMessages": 2,
                       "injectedInputs": 0, "textBlocks": 1, "textChars": len(HANDOFF), "thinkingBlocks": 1,
                       "model": "claude-sonnet-5-5"}
    assert SECRET not in json.dumps(receipt)
    novel = result(HANDOFF, stop_reason="new_reason", terminal_reason="new_terminal", subtype="new_subtype")
    stream.observe_result(novel)
    assert {stream.receipt[key] for key in ("stopReason", "terminalReason", "resultSubtype")} == {"other"}
    for model, label in (("claude sonnet", "other"), ("claude-sonnet-5-5", "claude-sonnet-5-5")):
        named = SummaryStream()
        named.observe(assistant(text("x"), model=model))
        assert named.summary()["model"] == label


def test_retry_notes_are_fixed_and_outside_the_quoted_history():
    state = claude_context.new_context()
    older = [{"turnId": "t", "messages": [{"role": "tool", "content": SECRET}]}]
    plain = claude_context.summary_prompt(state, older)
    header, data, _blank, request = plain.split("\n")
    assert header.startswith("Conversation data to summarize") and request == claude_context.SUMMARY_REQUEST
    assert json.loads(data) == {"previousSummary": "", "olderCompletedRounds": older}
    noted = claude_context.summary_prompt(state, older, note=claude_context.retry_note("truncated"))
    assert noted.startswith(plain + "\n\nBello retry note: ")
    assert claude_context.retry_note("refused") is None and claude_context.retry_note("provider_error") is None
    for category in ("truncated", "incomplete_response", "invalid_json", "invalid_shape", "empty_summary"):
        note = claude_context.retry_note(category)
        assert note and note.isascii() and SECRET not in note
    images = claude_context.summary_prompt(state, [{"turnId": "t", "messages": [
        {"role": "tool", "content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]}]}], note="fixed")
    assert images[0]["text"].endswith("\n\nBello retry note: fixed") and images[1]["type"] == "image"


def test_retry_notes_follow_the_detected_continuation():
    note = claude_context.retry_note
    assert note("incomplete_response", {"continuation": "no_visible_output"}).startswith(
        "A previous attempt produced no visible handoff text")
    assert "cut off mid-stream" in note("incomplete_response", {"continuation": "stream_cut"})
    for kind in ("second_response", "num_turns", "injected_input", None):
        assert note("incomplete_response", {"continuation": kind}) == note("truncated")
    assert note("truncated", {"continuation": "output_limit"}) == note("truncated")
    assert note("refused", {"continuation": "safety_stop"}) is None


def test_later_checkpoints_give_the_summarizer_the_original_task_as_context():
    state = claude_context.new_context()
    claude_context.begin_request(state, "original task " + SECRET)
    first = json.loads(claude_context.summary_prompt(state, []).split("\n")[1])
    assert "originalTask" not in first  # generation 0: the task is still inside the older rounds
    state.update(generation=2, summary="## Goals\nprior handoff")
    later = json.loads(claude_context.summary_prompt(state, []).split("\n")[1])
    assert later["originalTask"] == "original task " + SECRET and later["previousSummary"] == "## Goals\nprior handoff"


def test_failure_categories_are_closed():
    with pytest.raises(ValueError):
        SummaryRejected("model said something")
    assert not claude_context.retryable("input_too_large") and claude_context.retryable("truncated")
    assert claude_context.retryable("empty_summary") and claude_context.retryable("timeout")


# --- SDK / CLI contract ------------------------------------------------------


async def configured_thread(instance, tmp_path, policy=None, *, tools=False, async_tools=False):
    await instance.request("initialize", {})
    await instance.request("thread/start", {
        "threadId": "thread-1", "model": "sonnet", "cwd": str(tmp_path), "effort": "max",
        "noNeedForContext": policy or {"enabled": True, "thresholdTokens": 1000, "retainPercent": 10},
        "asyncTools": async_tools,
        "tools": [{"name": "read_file", "description": "Read one file.", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}] if tools else [],
    })


async def test_summarizer_cli_contract_is_plain_text_without_tools_schema_or_replay(tmp_path):
    instance = backend(tmp_path, FakeFactory(), [])
    try:
        await configured_thread(instance, tmp_path, tools=True)
        record = instance._record("thread-1")
        summary = instance._summary_options(record, {"effort": "max"})
        command = SubprocessCLITransport(prompt="", options=summary)._build_command()
        assert summary.output_format is None and "--json-schema" not in command
        assert command[command.index("--tools") + 1] == "" and "--allowedTools" not in command
        assert command[command.index("--max-turns") + 1] == "1"
        assert "--mcp-config" not in command and "--strict-mcp-config" in command
        assert "--replay-user-messages" not in command  # injected user turns are CLI-authored
        assert command[command.index("--model") + 1] == "sonnet"
        assert command[command.index("--effort") + 1] == "max"  # the role's requested effort
        assert command[command.index("--system-prompt") + 1] == claude_context.SUMMARY_SYSTEM
        # Forwarded verbatim as max_tokens by the managed CLI; adaptive
        # thinking shares it with the visible handoff.
        assert summary.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
        assert summary.resume is None and summary.session_id != record["claudeSessionId"]
        # Role turns that request a verdict still use the CLI's tool-based JSON schema.
        role = instance._options(record, {"effort": "max", "outputSchema": {"type": "object"}})
        role_command = SubprocessCLITransport(prompt="", options=role)._build_command()
        assert json.loads(role_command[role_command.index("--json-schema") + 1]) == {"type": "object"}
        assert "bello" in json.loads(role_command[role_command.index("--mcp-config") + 1])["mcpServers"]
    finally:
        await instance.stop()


def test_summary_budget_defaults_are_the_documented_contract():
    assert claude_context.summary_budget() == {
        "attempts": 2, "timeoutSeconds": 500, "stopTimeoutSeconds": 5,
        "maxOutputTokens": 64_000, "maxChars": 48_000, "targetChars": 16_000,
        "admissionMarginTokens": 8_192, "admissionEstimatePercent": 25,
    }
    # 64,000 is ONE combined output/reasoning cap. Characters are not tokens,
    # so no fixed share of it is claimed for reasoning; what is guaranteed is
    # that admission reserves the whole cap plus a margin before dispatch.
    admission = claude_context.summary_admission(1_000, 10_000, 1_000_000)
    assert admission == {"requiredTokens": 1_000 + 10_000 + 64_000 + 8_192 + 2_500, "marginTokens": 10_692,
                         "fits": True}
    # The bounded cleanup is a small fraction of the one summary deadline.
    assert claude_context.SUMMARY_STOP_TIMEOUT_SECONDS * 10 < claude_context.SUMMARY_TIMEOUT_SECONDS


def test_pinned_sdk_preserves_markdown_fences_in_result_text():
    import claude_agent_sdk
    from supervisor.runtime.claude_cli import MANAGED_RELEASES

    pinned = {release.sdk_version for release in MANAGED_RELEASES.values()}
    assert pinned == {claude_agent_sdk.__version__}, "re-verify the CLI summary contract for a new SDK pin"
    message = result(FENCED)
    assert isinstance(message, ResultMessage) and message.result == FENCED and message.structured_output is None
    assert isinstance(injected(OUTPUT_LIMIT_NOTICE), UserMessage)


def test_bundled_cli_contains_the_observed_continuation_notices():
    from supervisor.runtime.claude_cli import sdk_bundled_cli

    try:
        path = sdk_bundled_cli()
    except Exception:
        pytest.skip("this SDK wheel has no bundled CLI")
    with open(path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for needle in (b"Output token limit hit. Resume directly", b"stopped by a safety classifier",
                       b"exceeded the output token limit", b"cut off mid-stream", b"had no visible output",
                       b"Return structured output in the requested format"):
            assert data.find(needle) >= 0, needle


def test_managed_cli_contains_the_notices_and_deadlines_this_contract_relies_on():
    """Static check of the pinned managed CLI build, when it is prepared locally."""
    import re

    from supervisor.runtime.claude_cli import MANAGED_RELEASES, resolve_official_cli

    try:
        cli = resolve_official_cli(prepare=False)
    except Exception:
        pytest.skip("the pinned managed Claude Code CLI is not prepared on this machine")
    if cli.source != "managed-download":
        pytest.skip("no managed Claude Code build is pinned for this platform")
    assert cli.cli_version in {release.cli_version for release in MANAGED_RELEASES.values()}
    with open(cli.path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for needle in (b"Output token limit hit. Resume directly", b"exceeded the output token limit",
                       b"cut off mid-stream", b"had no visible output",
                       b"Your response above was stopped by a safety classifier"):
            assert data.find(needle) >= 0, needle
        # Nested deadlines: none is shorter than the 500-second summary
        # deadline for an active stream.
        assert re.search(rb"CLAUDE_STREAM_IDLE_TIMEOUT_MS\)\|\|0,300000\)", data)
        assert re.search(rb"API_TIMEOUT_MS,600000\)", data)
        assert re.search(rb"maxOutputTokensRecoveryCount:0,", data)


# --- Backend integration (fake SDK clients) ----------------------------------


class SummaryScriptClient(FakeClient):
    """Role: ``rounds`` tool rounds ending in the SDK's max_turns result, then a
    final answer. Summarizer: one scripted response per attempt."""

    script: list
    interrupt_task = None
    disconnect_cancelled = False
    disconnect_finished = False

    @property
    def summarizer(self):
        return self.options.system_prompt == claude_context.SUMMARY_SYSTEM

    async def get_context_usage(self):
        if self.summarizer:
            if self.factory.usage_gate is not None:
                self.factory.measuring.set()
                await self.factory.usage_gate.wait()
            return dict(self.factory.summary_usage)
        tokens = 100 if not self.options.resume else self.factory.curve(self.factory.main_queries)
        return {"totalTokens": tokens, "maxTokens": self.factory.max_tokens}

    async def get_mcp_status(self):
        if self.summarizer and self.factory.summarizer_mcp:
            return {"mcpServers": self.factory.summarizer_mcp}
        return await super().get_mcp_status()

    async def get_server_info(self):
        info = await super().get_server_info()
        if self.summarizer and self.factory.summarizer_account is not None:
            info["account"] = dict(self.factory.summarizer_account)
        return info

    async def query(self, prompt):
        if not isinstance(prompt, str):
            prompt = [message async for message in prompt]
        self.queries.append(prompt)
        session = self.options.resume or self.options.session_id
        if self.summarizer:
            index = len(self.factory.summary_prompts)
            self.factory.summary_prompts.append(prompt)
            self.script = list(self.factory.scripts[min(index, len(self.factory.scripts) - 1)])
            if self.factory.dispatch_gate is not None:
                # The prompt already reached the CLI; only the write's await is pending.
                await self.factory.dispatch_gate.wait()
            return
        self.factory.main_prompts.append((session, prompt))
        self.factory.main_queries += 1
        number = self.factory.main_queries
        if number <= self.factory.rounds:
            await self.messages.put(AssistantMessage(content=[
                TextBlock(text=f"private-older-evidence-{number}"),
                ToolUseBlock(id=f"call-{number}", name="mcp__bello__read_file", input={"path": f"file-{number}"}),
            ], model="sonnet"))
            await self.messages.put(UserMessage(content=[ToolResultBlock(tool_use_id=f"call-{number}",
                                                                         content=f"verbatim-result-{number}")]))
            limit = result_message(session_id=session, result=None, is_error=True, terminal_reason="max_turns")
            limit.subtype = "error_max_turns"
            await self.messages.put(limit)
        else:
            await self.messages.put(result_message(session_id=session, result="finished"))

    async def receive_response(self):
        if not self.summarizer:
            async for message in super().receive_response():
                yield message
            return
        while self.script:
            item = self.script.pop(0)
            if item is HANG:
                await asyncio.Event().wait()
            if item is GATE:
                await self.factory.release.wait()
                continue
            if isinstance(item, ResultMessage) and self.factory.before_result is not None:
                self.factory.before_result()
            yield item
            if isinstance(item, ResultMessage):
                return
        # The stream ended without a result.

    async def interrupt(self):
        self.interrupts += 1
        if self.summarizer:
            self.interrupt_task = asyncio.current_task()
            if self.factory.interrupt_error is not None:
                raise self.factory.interrupt_error
            self.script = list(self.factory.after_interrupt)

    async def disconnect(self):
        await super().disconnect()
        if not self.summarizer:
            return
        self.factory.disconnecting.set()
        if self.factory.disconnect_gate is not None:
            # e.g. the SDK's shielded transport close waiting for the CLI to exit
            try:
                await self.factory.disconnect_gate.wait()
            except asyncio.CancelledError:
                self.disconnect_cancelled = True
                raise
        if self.factory.disconnect_error is not None:
            raise self.factory.disconnect_error
        self.disconnect_finished = True


class SummaryScriptFactory(FakeFactory):
    def __init__(self, scripts, *, rounds=2, curve=None, max_tokens=200_000):
        super().__init__()
        self.scripts = scripts
        self.rounds = rounds
        self.curve = curve or (lambda queries: 100_000 if queries >= 2 else 100)
        self.max_tokens = max_tokens
        self.summary_usage = {"totalTokens": 100, "maxTokens": 200_000}
        self.summarizer_mcp = []
        self.summarizer_account = None
        self.main_queries = 0
        self.main_prompts = []
        self.summary_prompts = []
        # Summarizer fault and timing hooks for the cancellation tests.
        self.after_interrupt = [ABORTED]
        self.interrupt_error = None
        self.dispatch_gate = None
        self.usage_gate = None
        self.measuring = asyncio.Event()
        self.disconnect_gate = None
        self.disconnect_error = None
        self.disconnecting = asyncio.Event()
        self.release = asyncio.Event()
        self.before_result = None

    def __call__(self, options):
        client = SummaryScriptClient(self, options)
        self.clients.append(client)
        return client

    def summarizers(self):
        return [client for client in self.clients if client.summarizer]


def context_events(events):
    return [event for event in events if event["method"].startswith("thread/context")]


async def run_turn(instance, factory, tmp_path, events, policy=None):
    await configured_thread(instance, tmp_path, policy)
    original = instance._record("thread-1")["claudeSessionId"]
    await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                          "input": [{"type": "text", "text": "Implement the original request."}]})
    completed = await wait_completed(events)
    return original, completed["params"]["turn"]


async def test_plain_text_handoff_commits_the_checkpoint(tmp_path):
    factory = SummaryScriptFactory([answer()])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        record = instance._record("thread-1")
        assert record["contextState"]["generation"] == 1 and record["claudeSessionId"] != original
        assert record["contextState"]["summary"] == HANDOFF
        assert len(factory.summary_prompts) == 1
        bootstrap = factory.main_prompts[-1][1]
        # The handoff reaches the continuing session as readable text, not JSON-escaped.
        assert HANDOFF in bootstrap and "private-older-evidence-1" not in bootstrap
        assert "verbatim-result-2" in bootstrap and "Implement the original request." in bootstrap
        [compacted] = context_events(events)
        assert compacted["method"] == "thread/contextCompacted"
        params = compacted["params"]
        assert params["summaryFormat"] == "plain_text" and params["trigger"] == "threshold"
        assert params["summaryChars"] == len(HANDOFF) and params["summarizedRounds"] == 1
        assert params["summaryTimeoutSeconds"] == 500 and params["summaryMaxOutputTokens"] == 64_000
        assert params["freshTokens"] < params["previousTokens"]
        [attempt] = params["attempts"]
        assert attempt["outcome"] == "accepted" and attempt["summaryFormat"] == "plain_text"
        assert attempt["phase"] == "result" and attempt["textBlocks"] == 1 and attempt["thinkingBlocks"] == 1
        assert attempt["model"] == "claude-sonnet-5-5" and attempt["outputTokens"] == 20
        assert params["priorFailedCheckpoints"] == 0
        assert SECRET not in json.dumps(params) and "private-older-evidence" not in json.dumps(params)
        # Two max_turns slices, one summary session and the final answer, once each.
        assert turn["usage"]["input_tokens"] == 7 + 7 + 100 + 7
    finally:
        await instance.stop()


async def test_legacy_fenced_json_summary_still_commits(tmp_path):
    factory = SummaryScriptFactory([answer(FENCED)])
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        assert instance._record("thread-1")["contextState"]["summary"] == GOOD["summary"]
        [compacted] = context_events(events)
        assert compacted["params"]["summaryFormat"] == "legacy_fenced_json"
    finally:
        await instance.stop()


@pytest.mark.parametrize("script,outcomes", [
    (answer(REFUSAL_PADDED_WITH_HEADINGS), ["refused", "refused"]),
    (answer("\n".join(f"## {name}\nNone." for name in claude_context.SUMMARY_SECTIONS)), ["empty_summary"] * 2),
    (answer(HANDOFF, stop_reason="model_context_window_exceeded"), ["truncated", "truncated"]),
    (answer(HANDOFF, stop_reason="unexpected_new_stop"), ["unexpected_stop", "unexpected_stop"]),
    ([assistant(thinking()), assistant(text(HANDOFF), stop_reason="model_context_window_exceeded")],
     ["truncated", "truncated"]),
    ([assistant(text(HANDOFF), stop_reason="novel_stop")], ["unexpected_stop", "unexpected_stop"]),
    (answer("```json\n" + BARE), ["invalid_json", "invalid_json"]),
    ([assistant(text(HANDOFF)), result("## Critical data\nonly the last block")], ["incomplete_response"] * 2),
    ([assistant(thinking()), injected(NO_OUTPUT_NOTICE), assistant(text(HANDOFF), message_id="msg_summary_2"),
      result(HANDOFF, num_turns=2)], ["incomplete_response"] * 2),
    ([assistant(text(HANDOFF[:40])), injected(STREAM_CUT_NOTICE)], ["incomplete_response"] * 2),
    ([assistant(text("API Error: overloaded"), model="<synthetic>")], ["provider_error"] * 2),
    (answer(json.dumps({"status": "refused", "summary": ""})), ["refused", "refused"]),
    (answer(json.dumps({"status": "complete", "summary": ""})), ["empty_summary", "empty_summary"]),
    (answer(json.dumps({"status": "complete", "summary": "x" * 48_001})), ["oversized_summary", "oversized_summary"]),
    (answer(BARE, stop_reason="max_tokens"), ["truncated", "truncated"]),
    (CONTINUED, ["truncated", "truncated"]),
    ([assistant({"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": SECRET}})], ["tool_use", "tool_use"]),
    ([assistant(text("API Error: overloaded"), error="server_error")], ["provider_error", "provider_error"]),
    ([assistant(text(BARE)), result(BARE, subtype="error_max_turns", is_error=True, terminal_reason="max_turns")],
     ["max_turns", "max_turns"]),
    ([assistant(text(BARE[:20]))], ["no_result", "no_result"]),
])
async def test_rejected_summary_keeps_original_context_and_reports_categories(tmp_path, script, outcomes):
    factory = SummaryScriptFactory([script])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        record = instance._record("thread-1")
        state = record["contextState"]
        assert record["claudeSessionId"] == original and state["generation"] == 0 and state["summary"] == ""
        assert len(state["rounds"]) == 2 and not state["archivedSessions"]
        retained = json.dumps(state["rounds"])
        assert "private-older-evidence-1" in retained and "verbatim-result-2" in retained
        assert not (instance.state_dir / "context-archives").exists()
        # The next request continued the original native session, not a bootstrap.
        session, prompt = factory.main_prompts[-1]
        assert session == original and prompt == claude_context.CONTINUE_PROMPT
        [failed] = context_events(events)
        params = failed["params"]
        assert failed["method"] == "thread/contextCompactionFailed" and params["stage"] == "summary"
        assert [attempt["outcome"] for attempt in params["attempts"]] == outcomes
        assert params["category"] == outcomes[-1] and params["originalContextContinues"] is True
        assert params["consecutiveFailures"] == 1 and params["retryAtTokens"] == 100_000 + 100
        # The schedule is part of the persisted context state.
        assert (state["failedCheckpoints"], state["nextCheckTokens"]) == (1, 100_100)
        durable = json.loads(instance._state_file.read_text())["threads"]["thread-1"]["contextState"]
        assert (durable["failedCheckpoints"], durable["nextCheckTokens"]) == (1, 100_100)
        expected_note = True if claude_context.retry_note(outcomes[0]) else None
        assert params["attempts"][1].get("retryNote") is expected_note and "retryNote" not in params["attempts"][0]
        assert params["summaryTimeoutSeconds"] == 500 and params["summaryMaxOutputTokens"] == 64_000
        rendered = json.dumps(params)
        for private in (SECRET, "private-older-evidence", "verbatim-result", "first half", "API Error",
                        "overloaded", "Goals", "only the last block"):
            assert private not in rendered
        # The journalled copy keeps the same content-free diagnostics.
        journalled = _bounded_subagent_event_payload(failed["method"], params)
        assert [attempt["outcome"] for attempt in journalled["attempts"]] == outcomes
        assert all("phase" in attempt for attempt in journalled["attempts"])
    finally:
        await instance.stop()


async def test_cli_continuation_is_interrupted_and_its_usage_counted_once(tmp_path):
    factory = SummaryScriptFactory([CONTINUED, answer()])
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        assert instance._record("thread-1")["contextState"]["generation"] == 1
        first, second = factory.summarizers()
        # The continuation's complete-looking last fragment was never accepted;
        # the CLI was told to stop instead of finishing further paid continuations.
        assert first.interrupts == 1 and second.interrupts == 0
        assert first.disconnect_task is not None and second.disconnect_task is not None
        assert instance._record("thread-1")["contextState"]["summary"] == HANDOFF
        [compacted] = context_events(events)
        attempts = compacted["params"]["attempts"]
        assert [attempt["outcome"] for attempt in attempts] == ["truncated", "accepted"]
        assert attempts[0]["injectedInputs"] == 1 and attempts[0]["usageReported"] is True
        assert attempts[0]["continuation"] == "output_limit" and attempts[0]["phase"] == "streaming"
        assert attempts[1]["retryNote"] is True
        noted = factory.summary_prompts[1]
        assert noted.endswith("Bello retry note: " + claude_context.retry_note("truncated"))
        assert "Bello retry note" not in factory.summary_prompts[0]
        # Main slices 7+7+final 7; interrupted session 1000; accepted summary 100.
        # Per-response AssistantMessage usage (999,999) is never added.
        assert turn["usage"]["input_tokens"] == 7 + 7 + 7 + 1000 + 100
    finally:
        await instance.stop()


async def test_thinking_only_answer_gets_a_matching_corrective_note_then_commits(tmp_path):
    thinking_only = [assistant(thinking()), injected(NO_OUTPUT_NOTICE),
                     assistant(text(HANDOFF), message_id="msg_summary_2"), result(HANDOFF, num_turns=2)]
    factory = SummaryScriptFactory([thinking_only, answer()])
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        [compacted] = context_events(events)
        attempts = compacted["params"]["attempts"]
        assert [attempt["outcome"] for attempt in attempts] == ["incomplete_response", "accepted"]
        assert attempts[0]["continuation"] == "no_visible_output" and attempts[0]["textBlocks"] == 0
        expected = claude_context.retry_note("incomplete_response", {"continuation": "no_visible_output"})
        assert factory.summary_prompts[1].endswith("Bello retry note: " + expected)
    finally:
        await instance.stop()


@pytest.mark.parametrize("first,category", [
    ("## Goals\nNone.\n\n## Progress\nNone.\n\n## Next steps\nNone.", "empty_summary"),
    ('{"status":"complete","summary":"he said "hi" but', "invalid_json"),
])
async def test_second_attempt_can_commit_after_a_format_failure(tmp_path, first, category):
    factory = SummaryScriptFactory([answer(first), answer()])
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        assert instance._record("thread-1")["contextState"]["generation"] == 1
        [compacted] = context_events(events)
        attempts = compacted["params"]["attempts"]
        assert [attempt["outcome"] for attempt in attempts] == [category, "accepted"]
        assert factory.summary_prompts[1].endswith(claude_context.retry_note(category))
        if category == "invalid_json":  # where and why the legacy JSON broke, content-free
            assert attempts[0]["formatError"] == "expecting_delimiter" and attempts[0]["formatErrorAt"] > 30
        else:  # headings alone are not content; no section structure is required either way
            assert attempts[0]["textChars"] == len(first)
    finally:
        await instance.stop()


async def test_failed_checkpoint_waits_for_context_growth_then_commits(tmp_path):
    tokens = {2: 50_000, 3: 50_000, 4: 50_000, 5: 50_200}
    factory = SummaryScriptFactory([answer(BARE, stop_reason="max_tokens")] * 2 + [answer(FENCED)],
                                   rounds=5, curve=lambda queries: tokens.get(queries, 100))
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        # Rounds 3 and 4 were eligible but unchanged in size: no summary paid.
        assert len(factory.summary_prompts) == 3
        failed, compacted = context_events(events)
        assert failed["method"] == "thread/contextCompactionFailed"
        assert failed["params"]["retryAtTokens"] == 50_100 and failed["params"]["consecutiveFailures"] == 1
        assert compacted["method"] == "thread/contextCompacted"
        assert compacted["params"]["priorFailedCheckpoints"] == 1
        state = instance._record("thread-1")["contextState"]
        assert state["generation"] == 1
        # A commit starts a new generation with a clean schedule.
        assert state["failedCheckpoints"] == 0 and state["nextCheckTokens"] >= 1000
    finally:
        await instance.stop()


async def test_threshold_retries_stop_but_an_input_limit_checkpoint_is_still_attempted(tmp_path):
    tokens = {2: 50_000, 3: 50_150, 4: 50_400, 5: 60_000, 6: 70_000, 7: 196_000}
    factory = SummaryScriptFactory([answer("```json\n" + BARE)], rounds=7, curve=lambda queries: tokens.get(queries, 100))
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "failed"
        assert turn["error"]["message"] == (
            "Claude context checkpoint failed (invalid_json) and the original context cannot safely continue")
        failures = [event["params"] for event in context_events(events)]
        assert [params["trigger"] for params in failures] == ["threshold"] * 3 + ["inputLimit"]
        assert [params["consecutiveFailures"] for params in failures] == [1, 2, 3, 4]
        assert [params.get("retryAtTokens") for params in failures] == [50_100, 50_350, None, None]
        assert failures[2]["thresholdRetriesExhausted"] is True and failures[3]["thresholdRetriesExhausted"] is True
        assert failures[3]["originalContextContinues"] is False
        assert len(factory.summary_prompts) == 8  # four checkpoints, two attempts each
        assert factory.main_queries == 7
    finally:
        await instance.stop()


@pytest.mark.parametrize("setup,category", [
    (lambda factory: factory.summary_usage.update(maxTokens=1000), "input_too_large"),
    (lambda factory: factory.summary_usage.update(maxTokens=None), "usage_unavailable"),
    (lambda factory: factory.summarizer_mcp.append({"name": "bello", "status": "connected"}), "tools_exposed"),
    (lambda factory: setattr(factory, "summarizer_account", {"apiProvider": "bedrock"}), "connection_rejected"),
])
async def test_deterministic_precondition_failures_are_not_retried(tmp_path, setup, category):
    factory = SummaryScriptFactory([answer(BARE)])
    setup(factory)  # these overrides apply to summarizer connections only
    instance = backend(tmp_path, factory, events := [])
    try:
        await configured_thread(instance, tmp_path)
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Task."}]})
        turn = (await wait_completed(events))["params"]["turn"]
        assert turn["status"] == "completed"
        [failed] = context_events(events)
        assert [attempt["outcome"] for attempt in failed["params"]["attempts"]] == [category]
        assert factory.summary_prompts == [], "no summary query was sent"
        assert instance._record("thread-1")["contextState"]["generation"] == 0
    finally:
        await instance.stop()


async def test_summary_timeout_interrupts_the_unfinished_summarizer(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_context, "SUMMARY_TIMEOUT_SECONDS", 0.3)
    factory = SummaryScriptFactory([[assistant(text("{")), HANG]])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed"
        assert instance._record("thread-1")["claudeSessionId"] == original
        [failed] = context_events(events)
        # One deadline for the whole step: the attempt that spent it is not retried.
        attempts = failed["params"]["attempts"]
        assert [attempt["outcome"] for attempt in attempts] == ["timeout"]
        assert all(attempt["usageReported"] for attempt in attempts)
        assert [client.interrupts for client in factory.summarizers()] == [1]
        assert all(client.disconnect_task is not None for client in factory.summarizers())
        assert turn["usage"]["input_tokens"] == 7 * 3 + 1000
    finally:
        await instance.stop()


# --- Cancellation of a submitted summary --------------------------------------


async def start_summary(instance, factory, tmp_path):
    """Start a turn; return once its first summary query has been submitted."""
    await configured_thread(instance, tmp_path)
    baseline = asyncio.all_tasks()
    await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                          "input": [{"type": "text", "text": "Task."}]})
    async with asyncio.timeout(2):
        while not factory.summary_prompts:
            await asyncio.sleep(0)
    record = instance._record("thread-1")
    return record["claudeSessionId"], deepcopy(record["contextState"]), baseline


async def cancel_summary(instance, how):
    """One external cancellation of the active turn while its summary runs."""
    if how == "interrupt":
        response = await asyncio.wait_for(
            instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}), 2)
        assert response == {}
    elif how == "stop":
        await asyncio.wait_for(instance.stop(), 2)
    else:  # cancellation of the task that owns the session and drives the turn
        owner = instance._owners["thread-1"].task
        owner.cancel()
        await asyncio.wait({owner}, timeout=2)
        assert owner.cancelled(), "the cancellation is propagated, not swallowed"


def assert_cancelled_summary(instance, factory, events, turn, original, before, *, usage, interrupts=1, queries=1):
    """Cancellation, not a checkpoint failure: no receipt, backoff, retry or continuation."""
    assert turn["status"] == "interrupted"
    assert turn["usage"]["input_tokens"] == usage
    [summarizer] = factory.summarizers()
    assert summarizer.interrupts == interrupts
    if interrupts:
        # The explicit interrupt, drain and close run in the task that connected the client.
        assert summarizer.interrupt_task is summarizer.connect_task
    assert summarizer.disconnect_task is summarizer.connect_task and not summarizer.disconnect_cancelled
    assert len(factory.summary_prompts) == queries and factory.main_queries == 2
    assert context_events(events) == []
    record = instance._record("thread-1")
    # Not even the checkpoint schedule moved.
    assert record["claudeSessionId"] == original and record["contextState"] == before
    assert not (instance.state_dir / "context-archives").exists()


async def assert_no_stray_tasks(baseline):
    """No summary read, dispatch or control watcher outlives the cancelled turn."""
    stray = []
    for _ in range(200):
        stray = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        if not stray:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"tasks outlived the cancelled turn: {stray}")


@pytest.mark.parametrize("how", ["interrupt", "stop", "cancel"])
async def test_interrupt_during_summary_is_cancellation_not_a_checkpoint_failure(tmp_path, how):
    factory = SummaryScriptFactory([[HANG]])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        await cancel_summary(instance, how)
        turn = (await wait_completed(events))["params"]["turn"]
        # Coder slices 7 + 7 and the interrupted summary session's one terminal result (1000), each once.
        assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7 + 1000)
        assert "usageIncomplete" not in turn
        await assert_no_stray_tasks(baseline)
    finally:
        await instance.stop()


async def test_cancellation_during_the_retry_counts_both_summary_sessions_once(tmp_path):
    # The first attempt delivered a complete (but rejected) answer; the retry is cancelled mid-stream.
    factory = SummaryScriptFactory([answer("```json\n" + BARE), [HANG]])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        async with asyncio.timeout(2):
            while len(factory.summary_prompts) < 2:
                await asyncio.sleep(0)
        await cancel_summary(instance, "interrupt")
        turn = (await wait_completed(events))["params"]["turn"]
        first, second = factory.summarizers()
        assert (first.interrupts, second.interrupts) == (0, 1)  # only the unfinished session needed a stop
        # Slices 7 + 7, the rejected first answer (100) and the retry's aborted result (1000), each once.
        assert turn["status"] == "interrupted" and turn["usage"]["input_tokens"] == 7 + 7 + 100 + 1000
        assert "usageIncomplete" not in turn and context_events(events) == []
        record = instance._record("thread-1")
        assert record["claudeSessionId"] == original and record["contextState"] == before
        assert not (instance.state_dir / "context-archives").exists()
        await assert_no_stray_tasks(baseline)
    finally:
        await instance.stop()


async def test_terminal_result_reaching_the_waiting_read_is_counted_once(tmp_path):
    class SharedStreamClient(SummaryScriptClient):
        """Like the SDK, every reader of this summarizer shares one message stream."""

        async def receive_response(self):
            if not self.summarizer:
                async for message in super().receive_response():
                    yield message
                return
            while True:
                message = await self.messages.get()
                yield message
                if isinstance(message, ResultMessage):
                    return

        async def interrupt(self):
            self.interrupts += 1
            if self.summarizer:
                self.interrupt_task = asyncio.current_task()
                await self.messages.put(ABORTED)

    class SharedStreamFactory(SummaryScriptFactory):
        def __call__(self, options):
            client = SharedStreamClient(self, options)
            self.clients.append(client)
            return client

    factory = SharedStreamFactory([[]])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        await cancel_summary(instance, "interrupt")
        turn = (await wait_completed(events))["params"]["turn"]
        # The read that was already waiting receives the aborted result; it is
        # counted once and the fresh read is cancelled.
        assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7 + 1000)
        assert "usageIncomplete" not in turn
        await assert_no_stray_tasks(baseline)
    finally:
        await instance.stop()


@pytest.mark.parametrize("first", ["interrupt", "cancel"])
async def test_repeated_cancellation_during_the_stop_is_deferred_then_propagated(tmp_path, first):
    factory = SummaryScriptFactory([[HANG]])
    factory.after_interrupt = [GATE, ABORTED]  # the aborted result arrives late, within the stop bound
    instance = backend(tmp_path, factory, events := [])
    interrupt = None
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        owner = instance._owners["thread-1"].task
        [summarizer] = factory.summarizers()
        if first == "interrupt":
            interrupt = asyncio.create_task(
                instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}))
        else:
            owner.cancel()
        async with asyncio.timeout(2):
            while not summarizer.interrupts:
                await asyncio.sleep(0)
        for _ in range(2):  # cancel the session owner again while the summarizer drains
            owner.cancel()
            await asyncio.sleep(0.01)
        assert not owner.done() and summarizer.disconnect_task is None, "cleanup is deferred, not abandoned"
        factory.release.set()
        await asyncio.wait({owner}, timeout=2)
        assert owner.cancelled(), "the cancellation is propagated after the bounded cleanup"
        if interrupt is not None:
            assert await asyncio.wait_for(interrupt, 2) == {}, "the honored interrupt is acknowledged"
        turn = (await wait_completed(events))["params"]["turn"]
        assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7 + 1000)
        await assert_no_stray_tasks(baseline)
    finally:
        factory.release.set()
        if interrupt is not None and not interrupt.done():
            interrupt.cancel()
            await asyncio.gather(interrupt, return_exceptions=True)
        await instance.stop()


@pytest.mark.parametrize("after_interrupt", [[], [GATE, ABORTED]], ids=["missing", "late"])
async def test_unavailable_terminal_usage_is_flagged_never_invented(tmp_path, monkeypatch, after_interrupt):
    monkeypatch.setattr(claude_context, "SUMMARY_STOP_TIMEOUT_SECONDS", 0.3)
    factory = SummaryScriptFactory([[HANG]])
    factory.after_interrupt = after_interrupt
    instance = backend(tmp_path, factory, events := [])
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        await cancel_summary(instance, "interrupt")
        turn = (await wait_completed(events))["params"]["turn"]
        assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7)
        assert turn["usageIncomplete"] is True
        await assert_no_stray_tasks(baseline)
        factory.release.set()  # a result after the bound is never counted
        await asyncio.sleep(0.01)
        durable = json.loads(instance._state_file.read_text())["threads"]["thread-1"]["turns"][-1]
        assert durable["usage"] == turn["usage"] and durable["usageIncomplete"] is True
    finally:
        await instance.stop()


@pytest.mark.parametrize("fault", ["interrupt", "disconnect"])
async def test_summary_stop_cleanup_faults_keep_cancellation_semantics(tmp_path, fault):
    from claude_agent_sdk import CLIConnectionError

    factory = SummaryScriptFactory([[HANG]])
    setattr(factory, f"{fault}_error", CLIConnectionError("synthetic cleanup fault"))
    instance = backend(tmp_path, factory, events := [])
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        await cancel_summary(instance, "interrupt")
        turn = (await wait_completed(events))["params"]["turn"]
        if fault == "interrupt":  # attempted; that session's final usage is unavailable
            assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7)
            assert turn["usageIncomplete"] is True
        else:  # counted before the close failed
            assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7 + 1000)
            assert "usageIncomplete" not in turn
        await assert_no_stray_tasks(baseline)
    finally:
        await instance.stop()


@pytest.mark.parametrize("how", ["interrupt", "timeout"])
async def test_dispatch_cancelled_after_submission_still_stops_the_summary(tmp_path, monkeypatch, how):
    factory = SummaryScriptFactory([[HANG]])
    factory.dispatch_gate = asyncio.Event()  # submitted, but the query's await never returns
    instance = backend(tmp_path, factory, events := [])
    try:
        if how == "timeout":
            monkeypatch.setattr(claude_context, "SUMMARY_TIMEOUT_SECONDS", 0.3)
            original, turn = await run_turn(instance, factory, tmp_path, events)
            assert turn["status"] == "completed"
            assert instance._record("thread-1")["claudeSessionId"] == original
            [failed] = context_events(events)
            attempts = failed["params"]["attempts"]
            assert [attempt["outcome"] for attempt in attempts] == ["timeout"]
            assert all(attempt.get("usageReported") for attempt in attempts)
            assert [client.interrupts for client in factory.summarizers()] == [1]
            assert turn["usage"]["input_tokens"] == 7 * 3 + 1000 and "usageIncomplete" not in turn
        else:
            original, before, baseline = await start_summary(instance, factory, tmp_path)
            await cancel_summary(instance, "interrupt")
            turn = (await wait_completed(events))["params"]["turn"]
            assert_cancelled_summary(instance, factory, events, turn, original, before, usage=7 + 7 + 1000)
            await assert_no_stray_tasks(baseline)
    finally:
        factory.dispatch_gate.set()
        await instance.stop()


async def test_result_racing_an_interrupt_is_counted_once_without_a_stop(tmp_path):
    from supervisor.runtime.claude import _Command

    factory = SummaryScriptFactory([answer(BARE)])
    instance = backend(tmp_path, factory, events := [])
    acknowledged = []

    def interrupt_in_the_same_tick():
        factory.before_result = None
        future = asyncio.get_running_loop().create_future()
        acknowledged.append(future)
        instance._owners["thread-1"].queue.put_nowait(_Command("interrupt", {"turnId": "outer"}, future))

    factory.before_result = interrupt_in_the_same_tick
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        turn = (await wait_completed(events))["params"]["turn"]
        assert acknowledged and acknowledged[0].result() == {}
        # The delivered result is counted once, nothing is left to interrupt, nothing is committed.
        assert_cancelled_summary(instance, factory, events, turn, original, before,
                                 usage=7 + 7 + 100, interrupts=0)
        await assert_no_stray_tasks(baseline)
    finally:
        await instance.stop()


@pytest.mark.parametrize("how", ["interrupt", "cancel"])
async def test_cancellation_during_summarizer_close_lets_its_owning_task_finish(tmp_path, how):
    factory = SummaryScriptFactory([answer(BARE)])
    factory.disconnect_gate = asyncio.Event()  # the summarizer's transport close is still running
    instance = backend(tmp_path, factory, events := [])
    interrupt = None
    try:
        original, before, baseline = await start_summary(instance, factory, tmp_path)
        async with asyncio.timeout(2):
            await factory.disconnecting.wait()
        owner = instance._owners["thread-1"].task
        if how == "interrupt":
            interrupt = asyncio.create_task(
                instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}))
        else:
            owner.cancel()
        await asyncio.sleep(0.01)
        [summarizer] = factory.summarizers()
        assert not summarizer.disconnect_cancelled, "a raw cancellation must not abort the shielded close"
        factory.disconnect_gate.set()
        if interrupt is not None:
            assert await asyncio.wait_for(interrupt, 2) == {}
        else:
            await asyncio.wait({owner}, timeout=2)
            assert owner.cancelled()
        turn = (await wait_completed(events))["params"]["turn"]
        assert summarizer.disconnect_finished
        assert_cancelled_summary(instance, factory, events, turn, original, before,
                                 usage=7 + 7 + 100, interrupts=0)
        await assert_no_stray_tasks(baseline)
    finally:
        factory.disconnect_gate.set()
        if interrupt is not None and not interrupt.done():
            interrupt.cancel()
            await asyncio.gather(interrupt, return_exceptions=True)
        await instance.stop()


async def test_cancellation_before_summary_dispatch_sends_no_query(tmp_path):
    factory = SummaryScriptFactory([answer(BARE)])
    factory.usage_gate = asyncio.Event()  # the summarizer is still measuring its own context
    instance = backend(tmp_path, factory, events := [])
    interrupt = None
    try:
        await configured_thread(instance, tmp_path)
        record = instance._record("thread-1")
        original = record["claudeSessionId"]
        await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                              "input": [{"type": "text", "text": "Task."}]})
        async with asyncio.timeout(2):
            await factory.measuring.wait()
        before = deepcopy(record["contextState"])
        interrupt = asyncio.create_task(
            instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "outer"}))
        await asyncio.sleep(0.01)
        factory.usage_gate.set()  # the bounded setup step returns; the stop is honored before any query
        assert await asyncio.wait_for(interrupt, 2) == {}
        turn = (await wait_completed(events))["params"]["turn"]
        assert_cancelled_summary(instance, factory, events, turn, original, before,
                                 usage=7 + 7, interrupts=0, queries=0)
        assert "usageIncomplete" not in turn
    finally:
        factory.usage_gate.set()
        if interrupt is not None and not interrupt.done():
            interrupt.cancel()
            await asyncio.gather(interrupt, return_exceptions=True)
        await instance.stop()


async def test_a_resumed_thread_follows_the_persisted_checkpoint_schedule(tmp_path):
    """A restart neither repeats a failed checkpoint nor ignores the growth it asked for."""
    factory = SummaryScriptFactory([answer("```json\n" + BARE)])
    instance = backend(tmp_path, factory, events := [])
    try:
        original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed" and len(factory.summary_prompts) == 2  # one checkpoint, two attempts
        state = instance._record("thread-1")["contextState"]
        assert (state["failedCheckpoints"], state["nextCheckTokens"]) == (1, 100_100)
    finally:
        await instance.stop()
    durable = json.loads((tmp_path / "claude-state" / "threads.json").read_text())["threads"]["thread-1"]
    assert durable["claudeSessionId"] == original and durable["contextState"]["generation"] == 0
    assert (durable["contextState"]["failedCheckpoints"], durable["contextState"]["nextCheckTokens"]) == (1, 100_100)

    async def resumed_turn(curve_tokens, turn_id):
        resumed_factory = SummaryScriptFactory([answer(BARE)], rounds=0, curve=lambda _queries: curve_tokens)
        resumed = backend(tmp_path, resumed_factory, resumed_events := [])
        try:
            await resumed.request("thread/resume", {"threadId": "thread-1"})
            await resumed.request("turn/start", {"threadId": "thread-1", "turnId": turn_id,
                                                 "input": [{"type": "text", "text": "Continue."}]})
            assert (await wait_completed(resumed_events))["params"]["turn"]["status"] == "completed"
            return resumed_factory, resumed._record("thread-1")["contextState"]
        finally:
            await resumed.stop()

    # The same oversized context: the failing checkpoint is not paid for again.
    unchanged, state = await resumed_turn(100_000, "after-restart")
    assert unchanged.summary_prompts == [] and state["generation"] == 0
    assert (state["failedCheckpoints"], state["nextCheckTokens"]) == (1, 100_100)
    # Once the context has grown past the persisted bar, the checkpoint runs and commits.
    grown, state = await resumed_turn(100_500, "after-growth")
    assert len(grown.summary_prompts) == 1 and state["generation"] == 1 and state["failedCheckpoints"] == 0


async def test_a_resumed_thread_does_not_checkpoint_again_right_after_a_commit(tmp_path):
    factory = SummaryScriptFactory([answer()])
    instance = backend(tmp_path, factory, events := [])
    try:
        _original, turn = await run_turn(instance, factory, tmp_path, events)
        assert turn["status"] == "completed" and len(factory.summary_prompts) == 1
        state = instance._record("thread-1")["contextState"]
        fresh = context_events(events)[0]["params"]["freshTokens"]
        assert state["generation"] == 1 and state["nextCheckTokens"] == max(1000, fresh + 100)
    finally:
        await instance.stop()
    # The fresh session is still smaller than the committed bar: a resumed thread continues.
    resumed_factory = SummaryScriptFactory([answer()], rounds=0, curve=lambda _queries: 900)
    resumed = backend(tmp_path, resumed_factory, resumed_events := [])
    try:
        await resumed.request("thread/resume", {"threadId": "thread-1"})
        await resumed.request("turn/start", {"threadId": "thread-1", "turnId": "after-restart",
                                             "input": [{"type": "text", "text": "Continue."}]})
        assert (await wait_completed(resumed_events))["params"]["turn"]["status"] == "completed"
        assert resumed_factory.summary_prompts == [] and resumed._record("thread-1")["contextState"]["generation"] == 1
    finally:
        await resumed.stop()


async def test_smart_execution_late_result_survives_failed_then_successful_checkpoint(tmp_path):
    from types import SimpleNamespace

    curve = {"tokens": 100_000}
    factory = SummaryScriptFactory([answer("```json\n" + BARE)], curve=lambda _queries: curve["tokens"])
    instance = backend(tmp_path, factory, events := [])
    client = None
    try:
        await configured_thread(instance, tmp_path, tools=True, async_tools=True)
        record = instance._record("thread-1")
        record["activeTurnId"] = "outer"
        record["hasSession"] = True
        state = record["contextState"]
        claude_context.begin_request(state, "original task")
        claude_context.append_input(state, "old unrelated evidence")
        claude_context.finish_round(state, "outer")
        call = {"role": "assistant", "content": [{"id": "batch-call", "name": "run_parallel_tools", "input": {
            "calls": [{"name": "read_file", "arguments": {"path": "exact-original-path"}}]}}]}
        state["pending"].append(call)
        claude_context.append_input(state, "batch-call: original placeholders")
        claude_context.finish_round(state, "outer", async_pending=True)
        claude_context.append_input(state, "intervening response while results pending")
        claude_context.finish_round(state, "outer", async_pending=True)
        claude_context.append_input(state, "batch-call: final actual late result")
        instance._async_batches[("thread-1", "outer")] = SimpleNamespace(pending=False)
        before = deepcopy(state)
        session = record["claudeSessionId"]

        client, prompt = await instance._connect_context_client(record, {"turnId": "outer", "effort": "max"})
        assert prompt is None, "an unclosed fence is rejected; the original context continues"
        schedule = ("failedCheckpoints", "nextCheckTokens")
        assert ({k: v for k, v in record["contextState"].items() if k not in schedule}
                == {k: v for k, v in before.items() if k not in schedule})
        assert record["contextState"]["failedCheckpoints"] == 1 and record["claudeSessionId"] == session
        await client.disconnect()
        client = None

        client, prompt = await instance._connect_context_client(record, {"turnId": "outer", "effort": "max"})
        assert prompt is None and len(factory.summary_prompts) == 2, "unchanged size: deferred, no new summary"
        await client.disconnect()
        client = None

        curve["tokens"] = 100_500  # beyond the first retry step (+100 tokens)
        factory.scripts.append(answer(FENCED))
        client, prompt = await instance._connect_context_client(record, {"turnId": "outer", "effort": "max"})
        assert len(factory.summary_prompts) == 3
        candidate = record["contextState"]
        assert candidate["generation"] == 1 and candidate["summary"] == GOOD["summary"]
        assert "old unrelated evidence" not in prompt
        for retained in ("exact-original-path", "final actual late result", "intervening response while results pending"):
            assert retained in prompt
        assert candidate["openAsyncRoundStart"] == 0 and candidate["rounds"][0]["messages"][0] == call
        assert candidate["failedCheckpoints"] == 0
        methods = [event["method"] for event in context_events(events)]
        assert methods == ["thread/contextCompactionFailed", "thread/contextCompacted"]
    finally:
        instance._async_batches.clear()
        if client is not None:
            await client.disconnect()
        await instance.stop()


def test_controller_event_log_keeps_only_bounded_checkpoint_receipts():
    params = {
        "threadId": "thread-1", "message": "Context checkpoint failed; the original session was preserved.",
        "generation": 0, "trigger": "threshold", "stage": "summary", "category": "truncated",
        "previousTokens": 100_000, "projectedTokens": 100_000, "configuredThresholdTokens": 1000,
        "consecutiveFailures": 1, "retryAtTokens": 100_100, "thresholdRetriesExhausted": False,
        "originalContextContinues": True, "summary": SECRET,
        "attempts": [{"attempt": 1, "outcome": "truncated", "injectedInputs": 1, "durationMs": 3,
                      "usageReported": True, "assistantError": "rate limit with spaces", "text": SECRET}] * 6,
    }
    payload = _bounded_subagent_event_payload("thread/contextCompactionFailed", params)
    assert payload["category"] == "truncated" and payload["retry_at_tokens"] == 100_100
    assert payload["original_context_continues"] is True and "message" not in payload
    assert len(payload["attempts"]) == 4
    assert payload["attempts"][0] == {"attempt": 1, "outcome": "truncated", "injected_inputs": 1,
                                      "duration_ms": 3, "usage_reported": True}
    assert SECRET not in json.dumps(payload)
    compacted = _bounded_subagent_event_payload("thread/contextCompacted", {
        "generation": True, "previousTokens": -1, "summaryFormat": "fenced_json", "trigger": "x" * 65})
    assert compacted == {"summary_format": "fenced_json"}
