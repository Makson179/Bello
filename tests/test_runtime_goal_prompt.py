"""Neutral prompt contracts only: no providers, model calls, or live run reads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

import pytest

from supervisor.prompts import (
    build_adversary_prompt,
    build_cheap_runtime_prompt,
    build_coder_prompt,
    build_completion_review_prompt,
    build_restart_prompt,
    build_revision_prompt,
    build_stateless_supervisor_prompt,
)
from supervisor.prompts.supervisor import GOAL_CODER_GUIDANCE, GOAL_RUNTIME_GUIDANCE
from supervisor.schemas import SupervisorWakePacket


PROMPTS = Path(__file__).resolve().parents[1] / "supervisor" / "prompts" / "prompts.toml"
CHEAP_SECTION_SHA256 = "0f2fe7910121caaba2f7ce06bf0bf614de19804560ca69dd48bb6ef779a0e692"
APPROVAL_SECTIONS_SHA256 = "58e5a5255039f2b38aa72bf84192e3fbf8e3386b7d200543677e32e5873569b4"


def packet(**updates):
    base = SupervisorWakePacket(
        wake_sequence=12, latest_event_sequence=11, generation=1, restart_count=0,
        task_path="TASK.md", task_contents="Implement the requested behavior.",
        current_summary="Routine wake",
    )
    # Keeps these prompt-only tests independent of the controller/schema work
    # landing in parallel; no model validation or production packet is bypassed.
    return base.model_copy(update=updates)


def sections(**updates):
    payload = json.loads(build_stateless_supervisor_prompt(packet(**updates)))
    return dict(zip(payload["prompt_sections"], payload["instructions"], strict=True))


def raw_section(start, end):
    # The pinned hashes are of the repository's LF text; a Windows checkout may
    # convert prompts.toml to CRLF.
    raw = PROMPTS.read_bytes().replace(b"\r\n", b"\n")
    return raw[raw.index(start):raw.index(end)]


def test_cheap_runtime_section_is_byte_for_byte_unchanged(monkeypatch):
    monkeypatch.delenv("BELLO_PROMPTS_FILE", raising=False)
    raw = raw_section(b"[cheap_runtime]\n", b"[stateless_supervisor]\n")
    assert hashlib.sha256(raw).hexdigest() == CHEAP_SECTION_SHA256
    expected = tomllib.loads(PROMPTS.read_text(encoding="utf-8"))["cheap_runtime"]["text"].strip()
    payload = json.loads(build_cheap_runtime_prompt({"wake_reason": "synthetic event"}))
    assert payload["instructions"] == [expected]
    assert "advisory_to_coder" not in expected


def test_approval_safety_sections_are_byte_for_byte_unchanged():
    raw = raw_section(b"[stateless_supervisor.sections.approval]\n",
                      b"[stateless_supervisor.sections.action_review]\n")
    assert hashlib.sha256(raw).hexdigest() == APPROVAL_SECTIONS_SHA256


def test_old_defect_is_distinguished_from_current_evidence_without_attestation_gate():
    text = sections()["inputs"]
    assert "inspect the relevant current code yourself" in text
    assert "where useful" in text
    assert "older read is a reason to verify, not proof against newer work" in text
    assert "by sequence and artifact version" in text
    assert "path/lines or version you actually checked" in text
    assert "distinguish historical observations from current conclusions" in text
    assert "leave that allegation unverified" in text
    assert "independent, evidenced safety or approval problem" in text


def test_runtime_output_has_no_file_or_event_attestation_fields():
    blocks = sections(goal=True, goal_progress_review=True)
    text = "\n".join(blocks.values())
    for removed in ("observed_files", "evidence_sequences", "SHA-256", "sha256", "SAME buffer"):
        assert removed not in text
    assert "No file attestation or exact event-reference list is required for delivery" in blocks["inputs"]
    assert "changed/unavailable files invalidate the finding" not in text


def test_staleness_note_is_informational_and_never_a_delivery_or_pause_gate():
    blocks = sections()
    text = blocks["inputs"]
    assert "preparation took a long time or the coder changed its work meanwhile" in text
    assert "staleness note is informational and nonblocking" in text
    assert "coder checks whether the feedback still applies" in text
    assert "Do not withhold, retry, or turn feedback into a pause solely because of this note" in text
    assert "Staleness warnings do not block feedback delivery" in blocks["invariants"]


def test_user_constraints_are_not_confused_with_temporary_review_advice():
    blocks = sections()
    assert "Explicit user constraints and authorization boundaries remain binding until changed by the user" in blocks["inputs"]
    assert "satisfied, superseded, or made irrelevant" in blocks["inputs"]
    assert "Temporary reviewer diagnostics" in blocks["state_writes"]
    assert "are not standing constraints" in blocks["state_writes"]
    assert "Denied actions must not be retried or bypassed" in blocks["state_writes"]
    assert "never a temporary reviewer tactic or optional advice" in blocks["output_contract"]


def test_parallel_checks_and_independent_work_preserve_input_and_result_versions():
    text = sections()["inputs"]
    assert "Tests, builds, evaluations, and other checks" in text
    assert "while the coder continues independent work" in text
    assert "running check alone does not require the coder to stop editing" in text
    assert "isolated snapshot or unrelated files can proceed" in text
    assert "edits to live inputs may invalidate the result or require coordination" in text
    assert "result tied to the version actually checked" in text
    assert "input isolation or version binding is unknown" in text
    assert "Preserve protected artifacts and explicit user constraints" in text


def test_general_restart_needs_current_evidence_and_goal_exploration_rules_are_scoped():
    text = sections(current_summary="Coder completed action")["action_review"]
    assert "current, verified evidence" in text
    assert "The number of prior corrections" in text
    assert "or an old inspection alone is not enough" in text
    assert "Goal plateau" not in text
    assert "score plateau" not in text
    assert "single check fails once" not in text
    assert "several coherent attempts" not in text
    assert "confirm the baseline" not in text
    assert "needs repeated correction across the run" not in text
    example, _ = json.JSONDecoder().raw_decode(text.split("Output: ", 1)[1])
    assert example["decision"] == "noop"
    assert example["handoff"] is None
    assert example["persistent_decision"] is None
    assert example["advisory_to_coder"] is None


@pytest.mark.parametrize("goal,progress_review,expected", [
    (False, False, False), (False, True, False), (True, False, True), (True, True, True),
])
def test_runtime_goal_guidance_is_explicitly_gated(goal, progress_review, expected):
    blocks = sections(goal=goal, goal_progress_review=progress_review)
    assert ("goal_runtime" in blocks) is expected
    assert ("action_review" in blocks) is (goal and progress_review)
    if expected:
        assert blocks["goal_runtime"] == GOAL_RUNTIME_GUIDANCE
    else:
        assert GOAL_RUNTIME_GUIDANCE not in blocks.values()


def test_goal_progress_guidance_checks_relevant_map_and_current_work_without_forcing_a_pivot():
    text = sections(goal=True, goal_progress_review=True)["goal_runtime"]
    for phrase in (
        "read the relevant map entries", "current work and available results",
        "attempt/version identities", "meaningfully changed hypothesis",
        "A flat score alone does not establish a loop", "not on every wake",
        "a single failed check", "unfinished work", "several coherent attempts",
        "an unchanged retest", "during Goal exploration",
        "Changed hypotheses and meaningful diagnostics count as progress",
        "optional", "decision=noop", "advisory_to_coder",
        "up to 2-3 short strategic ideas", "not a detailed implementation plan",
        "coder may ignore the ideas", "without having to justify that choice",
        "Never require a pivot solely because the score has plateaued",
        "not an intervention, restart reason, standing constraint",
        "keep message_to_coder and persistent_decision null", "short supportive phrase",
        "Do not launch a new evaluation", "map is absent or evidence is insufficient",
    ):
        assert phrase in text


def test_runtime_advisory_is_separate_from_intervention_and_approval():
    text = sections()["output_contract"]
    assert "message_to_coder: for intervene" in text
    assert "human-directed intervene or pause, quote the human text verbatim" in text
    assert "pause does not send it to the coder" in text
    assert "advisory_to_coder: only with noop" in text
    assert "not a corrective instruction" in text
    assert "Otherwise null" in text
    assert "does not interrupt the coder" in text
    assert "up to 2-3 short strategic ideas, which the coder may ignore" in text


def test_optional_advice_preserves_stable_key_and_thirty_minute_deduplication():
    text = sections()["output_contract"]
    assert "stable key naming the compared attempts/approach" in text
    assert "not this wake or timestamp" in text
    assert "same key for the 30-minute deduplication policy" in text
    assert "do not reword it into a new key to repeat it sooner" in text


@pytest.mark.parametrize("builder", [build_coder_prompt, build_restart_prompt, build_revision_prompt])
def test_goal_map_guidance_is_incremental_and_archives_only_when_needed(tmp_path, builder):
    args = (tmp_path / "TASK.md", "Address the required defect") if builder is build_revision_prompt else (tmp_path / "TASK.md",)
    ordinary = builder(*args)
    guided = builder(*args, goal=True)
    assert guided == GOAL_CODER_GUIDANCE + "\n\n" + ordinary
    assert len(GOAL_CODER_GUIDANCE) < 700
    assert "incremental edits, not a whole-map rewrite" in guided
    assert "Compact only when the map grows unwieldy" in guided
    assert "append-only archive with attempt/version pointers" in guided
    assert "Never erase past outcomes" in guided
    assert "append-only archive" not in ordinary
    assert not (tmp_path / "GOAL_MAP.md").exists()


def test_runtime_goal_guidance_does_not_change_completion_or_adversary_instructions():
    current = packet(goal=True, goal_progress_review=True)
    assert GOAL_RUNTIME_GUIDANCE not in build_completion_review_prompt(current)
    assert GOAL_RUNTIME_GUIDANCE not in build_adversary_prompt(current)
