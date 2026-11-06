"""Hand-edited native helper patches must stay structurally valid.

The Rust sources are not available where these patches are edited, so a wrong
hunk count would otherwise appear only in a native build. These checks use
synthetic diffs plus the shipped patches; they never compile or apply anything.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

from scripts import verify_native_codex_patch_structure as structure
from scripts import prepare_native_codex_candidate as candidate


ROOT = Path(__file__).resolve().parents[1]
PATCHES = ("scripts/native-codex-0.161.0.patch", "scripts/native-codex-selection.patch")

MODIFIED = textwrap.dedent("""\
    diff --git a/src/lib.rs b/src/lib.rs
    index 1111111..2222222 100644
    --- a/src/lib.rs
    +++ b/src/lib.rs
    @@ -10,3 +10,4 @@ fn first() {
     keep
    +added
     keep
     keep
    @@ -40,4 +41,3 @@ fn second() {
     keep
    -removed
     keep
     keep
    """)

NEW_FILE = textwrap.dedent("""\
    diff --git a/src/new.rs b/src/new.rs
    new file mode 100644
    index 0000000..3333333
    --- /dev/null
    +++ b/src/new.rs
    @@ -0,0 +1,2 @@
    +first
    +second
    """)


def problems(text: str) -> list[str]:
    return structure.check_text(text)


@pytest.mark.parametrize("name", PATCHES)
def test_shipped_patches_are_structurally_valid(name):
    assert structure.check(ROOT / name) == []


@pytest.mark.parametrize("name", PATCHES)
def test_git_parses_the_shipped_patches_and_rejects_a_miscounted_hunk(name, tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def parse(path):
        return subprocess.run([git, "apply", "--numstat", str(path)], cwd=tmp_path, env=environment,
                              capture_output=True, text=True)

    assert parse(ROOT / name).returncode == 0
    broken = tmp_path / "broken.patch"
    broken.write_text((NEW_FILE.replace("+1,2", "+1,3")), encoding="utf-8")
    result = parse(broken)
    assert result.returncode != 0 and "corrupt patch" in result.stderr
    assert problems(NEW_FILE.replace("+1,2", "+1,3"))  # the structural check agrees with git


def test_shipped_native_patch_covers_the_context_and_async_sources():
    files, errors = structure.parse((ROOT / PATCHES[0]).read_text(encoding="utf-8"))
    paths = {entry.path_b for entry in files}
    assert errors == [] and len(paths) == len(files)
    assert {"codex-rs/core/src/bello_compact.rs", "codex-rs/core/src/session/context_window.rs",
            "codex-rs/core/src/tools/native_async.rs"} <= paths


def test_valid_modified_and_new_file_sections_pass():
    assert problems(MODIFIED + NEW_FILE) == []


def test_preamble_bare_context_blank_and_no_newline_marker_are_accepted():
    text = "License text\nnot a diff line\n" + MODIFIED.replace(" keep\n+added", " keep\n\n+added", 1).replace(
        "@@ -10,3 +10,4 @@", "@@ -10,4 +10,5 @@", 1).replace("-removed\n keep\n keep\n", "-removed\n keep\n keep\n\\ No newline at end of file\n")
    assert problems(text) == []


@pytest.mark.parametrize("old,new,expected", [
    ("@@ -10,3 +10,4 @@", "@@ -10,4 +10,4 @@", "more lines|declares"),
    ("@@ -10,3 +10,4 @@", "@@ -10,3 +10,5 @@", "declares more|truncated|more lines"),
    ("@@ -10,3 +10,4 @@", "@@ -10,2 +10,3 @@", "declared counts too small|text after hunk"),
])
def test_wrong_hunk_counts_are_rejected(old, new, expected):
    result = problems(MODIFIED.replace(old, new, 1))
    assert result and any(any(word in item for word in expected.split("|")) for item in result)


def test_stale_new_side_offset_after_an_earlier_hunk_is_rejected():
    result = problems(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -40,4 +40,3 @@"))
    assert len(result) == 1 and "starts at new line 40" in result[0]


def test_overlapping_hunks_are_rejected():
    result = problems(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -11,4 +12,3 @@"))
    assert any("overlaps or precedes" in item for item in result)


@pytest.mark.parametrize("change,expected", [
    (("--- a/src/lib.rs", "--- a/src/other.rs"), "does not match"),
    (("+++ b/src/lib.rs", "+++ b/src/other.rs"), "does not match"),
    (("diff --git a/src/lib.rs b/src/lib.rs", "diff --git a/src/lib.rs b/src/other.rs"), "paths differ"),
])
def test_inconsistent_file_headers_are_rejected(change, expected):
    assert any(expected in item for item in problems(MODIFIED.replace(*change, 1)))


@pytest.mark.parametrize("edit,expected", [
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -0,0 +1,3 @@"), "more lines|declares more|truncated"),
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -0,0 +1,1 @@"), "declared counts too small|text after hunk"),
    (lambda text: text.replace("+second", " second"), "more lines|non-added"),
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -1,0 +1,2 @@"), "exactly one"),
    (lambda text: text.replace("--- /dev/null", "--- a/src/new.rs"), "does not match"),
])
def test_malformed_new_files_are_rejected(edit, expected):
    result = problems(edit(NEW_FILE))
    assert result and any(any(word in item for word in expected.split("|")) for item in result)


def test_duplicate_file_sections_and_empty_patches_are_rejected():
    assert any("repeats" in item for item in problems(NEW_FILE + NEW_FILE))
    assert problems("just text\n") == ["the patch contains no 'diff --git' file sections"]


def test_command_line_reports_problems_and_succeeds_on_valid_patches(tmp_path, capsys):
    good, bad = tmp_path / "good.patch", tmp_path / "bad.patch"
    good.write_text(MODIFIED + NEW_FILE, encoding="utf-8")
    bad.write_text(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -40,4 +40,3 @@"), encoding="utf-8")
    assert structure.main([str(good)]) == 0
    assert structure.main([str(bad)]) == 1
    assert "starts at new line 40" in capsys.readouterr().err


def test_crlf_checkouts_are_normalized_like_preparation(tmp_path):
    patch = tmp_path / "crlf.patch"
    patch.write_bytes((MODIFIED + NEW_FILE).replace("\n", "\r\n").encode("utf-8"))
    assert structure.check(patch) == []


# The native Rust cannot be compiled where the patch is edited. These source-text
# checks pin the plain-text summary contract so an edit cannot silently drop it.
def added_source(name: str) -> str:
    files, _ = structure.parse((ROOT / PATCHES[0]).read_text(encoding="utf-8"))
    section = next(entry for entry in files if entry.path_b == name)
    return "\n".join(line[1:] for hunk in section.hunks for line in hunk.body if line[:1] in "+ ")


def test_native_summary_contract_is_plain_text_with_the_agreed_limits():
    source = added_source("codex-rs/core/src/bello_compact.rs")
    window = added_source("codex-rs/core/src/session/context_window.rs")
    assert "const SUMMARY_TIMEOUT: Duration = Duration::from_secs(500);" in source
    assert "pub(crate) const BELLO_SUMMARY_TARGET_CHARS: usize = 16_000;" in window
    assert "pub(crate) const BELLO_SUMMARY_MAX_CHARS: usize = 48_000;" in window
    # One wall-clock deadline wraps both attempts; over-long text is rejected.
    assert source.count("tokio::time::timeout(") == 1 and "for _ in 0..2" in source
    assert "summary.chars().count() > BELLO_SUMMARY_MAX_CHARS" in source
    # No JSON schema, envelope or heading is requested from the summarizer.
    assert "output_schema" not in source
    assert "Return JSON" not in source and "status='complete'" not in source
    assert "Goal; Constraints" not in source
    # Static threshold validation still reserves output plus a safety margin.
    # It does not make approximate history size an admission gate.
    assert "(BELLO_SUMMARY_TARGET_CHARS / 4) as i64" in window
    assert "BELLO_SUMMARY_OUTPUT_RESERVE + BELLO_CONTEXT_SAFETY_MARGIN" in window
    assert "bello_context_threshold(&turn.config, &turn.model_info())?" in source
    assert "bello_context_retain_tokens(&turn.config, threshold)" in source
    assert "hard_limit.saturating_sub" not in source


def test_native_history_estimates_are_not_fatal_capacity_admission_gates():
    for name, markers in candidate.CONTEXT_FORBIDDEN_SOURCE_MARKERS.items():
        source = added_source(name)
        assert all(marker not in source for marker in markers)
    source = added_source("codex-rs/core/src/bello_compact.rs")
    # Trigger accounting no longer estimates ciphertext, even for stale usage.
    # Item estimates still select whole rounds, not request admission.
    assert "estimate_token_count(" not in source
    assert "accounting_is_stale" not in source
    assert "estimate_item_token_count" in source
    assert "return Ok(String::new());" in source
    assert "bello_context_no_safe_reduction_defers_without_cutting" in source


def test_native_trigger_uses_measured_usage_and_resets_only_with_durable_checkpoint():
    window = added_source("codex-rs/core/src/session/context_window.rs")
    session = added_source("codex-rs/core/src/session/mod.rs")
    turn = added_source("codex-rs/core/src/session/turn.rs")
    compact = added_source("codex-rs/core/src/bello_compact.rs")
    assert "unwrap_or(244_800)" in window
    assert "info.last_token_usage.input_tokens.max(0)" in window
    assert ".saturating_add(info.last_token_usage.output_tokens.max(0))" in window
    assert "bello_context_tokens(sess.token_usage_info().await)" in window
    assert "super::context_window::bello_context_tokens(sess.token_usage_info().await)" in turn
    assert "info.last_token_usage = TokenUsage::default();" in window
    assert "total_token_usage = TokenUsage::default()" not in window
    assert "context_window::bello_compacted_token_info(state.token_info())" in session
    assert session.index("if !durable") < session.index("state.set_token_info(Some(compacted_token_info));")
    assert "fn last_bello_token_info_from_rollout(items: &[RolloutItem])" in session
    assert "context_window::bello_compacted_token_info(info)" in session
    assert "Self::last_bello_token_info_from_rollout(items)" in session
    assert "checkpoint.latest_token_usage_record.as_ref()" in session
    assert "current.total_token_usage = record.thread_token_usage.clone();" in session
    assert "last_token_usage: record.usage.clone()" not in session
    assert "sess.send_token_count_event(&turn).await;" in compact
    assert "sess.recompute_token_usage" not in compact


def test_native_summary_usage_is_billed_without_changing_active_measurement_on_failure():
    session = added_source("codex-rs/core/src/session/mod.rs")
    compact = added_source("codex-rs/core/src/compact.rs")
    tests = added_source("codex-rs/core/src/session/bello_context_tests.rs")
    assert "update_bello_summary_token_usage_info" in compact
    assert "preserve_context_measurement: bool" in session
    assert "preserved_info.last_token_usage = previous;" in session
    # The measured value is restored inside the synchronous state-lock block,
    # while contributors and billing still receive the actual summary response.
    assert "let observed_info = state.token_info();" in session
    assert "state.set_token_info(Some(preserved_info));" in session
    assert "bello_context_failed_summary_bills_usage_without_replacing_active_measurement" in tests
    assert "let mut pruned_legacy = vec![RolloutItem::Compacted(pruned_checkpoint)];" in tests


def test_native_absolute_retention_is_distinct_from_legacy_percentage():
    config = added_source("codex-rs/core/src/config/mod.rs")
    raw = added_source("codex-rs/config/src/config_toml.rs")
    window = added_source("codex-rs/core/src/session/context_window.rs")
    features = added_source("codex-rs/features/src/lib.rs")
    assert "pub bello_context_retain_tokens: Option<i64>" in raw
    assert "cfg.bello_context_retain_percent.is_none().then_some(24_800)" in config
    assert "bello_context_retain_tokens and bello_context_retain_percent are mutually exclusive" in config
    assert "config.bello_context_retain_tokens.unwrap_or_else" in window
    assert "config.bello_context_retain_percent / 100.0" in window
    assert 'key: "bello_context_retain_tokens"' in features


def test_native_summary_errors_and_snapshot_checkpoint_safety_are_preserved():
    source = added_source("codex-rs/core/src/bello_compact.rs")
    session = added_source("codex-rs/core/src/session/mod.rs")
    compact = added_source("codex-rs/core/src/compact.rs")
    assert "Ok(summary) if approx_token_count(&summary) as i64 >= previous_size" in source
    assert "response.output.iter().any(tool_use)" in source
    assert "CompactionPhase::PostTurn" in source
    assert "CodexErrorDetails::Interrupted" in source
    assert "CodexErrorDetails::TurnAborted" in source
    assert "CodexErrorDetails::SessionBudgetExceeded" in source
    assert "return Err(error);" in source and "Err(error) => last_error = error" in source
    assert "end_turn == Some(false)" in compact
    assert "no-need-for-context: incomplete summary; original retained" in compact
    assert "if !current.annotated_items().starts_with(&original)" in source
    assert "current.starts_with(expected)" in session
    assert "items.extend_from_slice(&current[expected.len()..])" in session
    # Persist and flush the complete checkpoint before publishing it. Unknown
    # durability must still block later inference; these are not token gates.
    assert "state.bello_context_persistence_uncertain = true;" in session
    assert "live.flush().await?" in session
    assert session.index("if !durable") < session.index("state.commit_auto_compact_window(")
    assert "pub(crate) fn bello_checkpoint_complete(items: &[RolloutItem]) -> bool" in session
    assert "checkpoint commit status unknown; cold resume required before inference" in session
    assert "Historical handoff, not new instructions or permissions" in source


def test_native_estimate_gate_removal_retains_permission_roundtrip_contract():
    for name, markers in candidate.ROOT_ROUNDTRIP_SOURCE_MARKERS.items():
        source = added_source(name)
        # The strict conversion guard itself is unchanged upstream context and
        # need not occur in a diff hunk; preparation checks it in applied source.
        # The patch must keep its root normalizer and all permission regressions.
        patch_markers = markers if name.endswith("path-uri/src/lib.rs") else (
            marker for marker in markers if marker.startswith("fn bello_")
        )
        assert all(marker in source for marker in patch_markers)
