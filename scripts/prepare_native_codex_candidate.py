#!/usr/bin/env python3
"""Prepare the explicit, unpublished Codex 0.161.0 source candidate only.

This helper neither downloads nor compiles anything. Its source-input identity
is not a binary manifest, proof receipt, or automatic-install pin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_windows as common

UPSTREAM_REVISION = "979011409de0a60b52f179721948e65531d26144"
VERSION = "0.161.0"
RUST_VERSION = "1.95.0"
V8_RELEASE = "rusty-v8-v150.4.0"
PATCH = "scripts/native-codex-0.161.0.patch"
INPUTS = (PATCH, "scripts/prepare_native_codex_candidate.py",
          "scripts/prepare_native_codex_windows.py")
SCHEMA = "bello.native-codex-source-candidate.v1"
ROOT_ROUNDTRIP_SOURCE_MARKERS = {
    "codex-rs/utils/path-uri/src/lib.rs": (
        "let url = with_canonical_windows_drive_root(url);",
        "fn with_canonical_windows_drive_root(mut url: Url) -> Url",
        "file_url_for_native_conversion(&self.0)",
    ),
    "codex-rs/protocol/src/permissions.rs": (
        "if path_uri_from_raw(raw_path.clone()).as_ref() == Ok(&path)",
        'Err("permission path cannot be represented losslessly".to_string())',
        "fn bello_permission_path_roundtrip_config_drive_roots()",
        "fn bello_permission_path_roundtrip_profile_preserves_read_write_and_deny()",
        "fn bello_permission_path_roundtrip_keeps_lossy_inputs_rejected()",
        "fn bello_permission_path_roundtrip_native_windows_root()",
    ),
}
CONTEXT_SOURCE_MARKERS = {
    "codex-rs/core/src/bello_compact.rs": (
        "fn checked_summary(", "fn plan(", "async fn run(",
        "replace_compacted_history_from_snapshot(", "pending_call_ids()",
        # Plain-text summary, one 500 s deadline for both attempts, and rejection
        # (never truncation) of a summary over the maximum length.
        "const SUMMARY_TIMEOUT: Duration = Duration::from_secs(500);",
        "tokio::time::timeout(SUMMARY_TIMEOUT, attempts)",
        "summary.chars().count() > BELLO_SUMMARY_MAX_CHARS",
        # Estimates still select the soft tail and reject a non-reducing
        # summary; they must not admit/refuse requests as a capacity oracle.
        "bello_context_threshold(&turn.config, &turn.model_info())?",
        "bello_context_retain_tokens(&turn.config, threshold)",
        "sess.send_token_count_event(&turn).await;",
        "sess.ensure_bello_context_persistence().await?",
        "CompactionPhase::PostTurn",
        "Ok(summary) if approx_token_count(&summary) as i64 >= previous_size",
        "CodexErrorDetails::Interrupted",
        "CodexErrorDetails::TurnAborted",
        "CodexErrorDetails::SessionBudgetExceeded",
        "Err(error) => last_error = error",
        "if !current.annotated_items().starts_with(&original)",
    ),
    "codex-rs/core/src/session/context_window.rs": (
        # 16,000-character prompt target, 48,000-character limit and the input
        # budget that leaves the summary output reserve plus a safety margin.
        "pub(crate) const BELLO_SUMMARY_TARGET_CHARS: usize = 16_000;",
        "pub(crate) const BELLO_SUMMARY_MAX_CHARS: usize = 48_000;",
        "pub(crate) const BELLO_CONTEXT_SAFETY_MARGIN: i64 = 4_096;",
        "pub(crate) fn bello_input_budget(",
        "unwrap_or(244_800)",
        "pub(crate) fn bello_context_retain_tokens(",
        "pub(crate) fn bello_context_tokens(info: Option<TokenUsageInfo>) -> i64",
        "info.last_token_usage.input_tokens.max(0)",
        ".saturating_add(info.last_token_usage.output_tokens.max(0))",
        "pub(crate) fn bello_compacted_token_info(",
        "info.last_token_usage = TokenUsage::default();",
        "bello_context_tokens(sess.token_usage_info().await)",
    ),
    "codex-rs/core/src/session/mod.rs": (
        "async fn replace_compacted_history_from_snapshot(",
        "current.starts_with(expected)",
        "items.extend_from_slice(&current[expected.len()..])",
        "state.bello_context_persistence_uncertain = true;",
        "live.flush().await?",
        "state.commit_auto_compact_window(metadata.window_number, metadata.window_ids)",
        "pub(crate) fn bello_checkpoint_complete(items: &[RolloutItem]) -> bool",
        "fn last_bello_token_info_from_rollout(items: &[RolloutItem]) -> Option<TokenUsageInfo>",
        "context_window::bello_compacted_token_info(state.token_info())",
        "state.set_token_info(Some(compacted_token_info));",
        "Self::last_bello_token_info_from_rollout(items)",
        "current.total_token_usage = record.thread_token_usage.clone();",
        "checkpoint.latest_token_usage_record.as_ref()",
        "pub(crate) async fn update_bello_summary_token_usage_info(",
        "preserve_context_measurement: bool",
        "preserved_info.last_token_usage = previous;",
    ),
    "codex-rs/core/src/session/turn.rs": (
        "super::context_window::bello_context_tokens(sess.token_usage_info().await)",
    ),
    "codex-rs/core/src/compact.rs": (
        'bello_compact.rs', "codex_features::Feature::BelloContextCompaction",
        "return bello::run(",
        "sess.update_bello_summary_token_usage_info(turn_context, token_usage.as_ref()).await?;",
    ),
    "codex-rs/features/src/lib.rs": (
        'key: "bello_context_compaction",\n        stage: Stage::UnderDevelopment,\n        default_enabled: false,',
        'key: "bello_context_retain_tokens",\n        stage: Stage::UnderDevelopment,\n        default_enabled: false,',
    ),
    "codex-rs/config/src/config_toml.rs": (
        "pub bello_context_retain_percent: Option<f64>",
        "pub bello_context_retain_tokens: Option<i64>",
    ),
    "codex-rs/core/src/config/mod.rs": (
        "cfg.bello_context_retain_percent.is_none().then_some(24_800)",
        "bello_context_retain_tokens and bello_context_retain_percent are mutually exclusive",
    ),
}

# These are the removed native history-estimate admission gates, not the static
# validation of a configured threshold against reported model capacity. Keep
# this rejection contract separate from positive markers so an old helper
# cannot satisfy preparation merely by retaining the surviving safety checks.
CONTEXT_FORBIDDEN_SOURCE_MARKERS = {
    "codex-rs/core/src/bello_compact.rs": (
        "bello_input_budget",
        "summarization input exceeds safe model budget",
        "protected context fills model capacity",
        "cannot verify replacement context size",
        "protected prefix and whole recent rounds exceed safe model budget",
        "projected.estimate_token_count",
        "accounting_is_stale",
        "estimate_token_count(",
        "sess.recompute_token_usage(&turn).await",
    ),
    "codex-rs/core/src/session/mod.rs": (
        "late results exceed safe replacement budget",
    ),
}


def verify_root_roundtrip_source(source: Path) -> None:
    """Refuse missing repair/tests; exact source provenance is checked separately."""
    for name, markers in ROOT_ROUNDTRIP_SOURCE_MARKERS.items():
        path = source / name
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0) & 0x400):
            raise ValueError("Candidate root-roundtrip source must be a regular file")
        contents = path.read_text(encoding="utf-8")
        if any(marker not in contents for marker in markers):
            raise ValueError("Candidate root-roundtrip source contract is incomplete")


def verify_context_source(source: Path) -> None:
    """Require opt-in routing and guarded replacement, not proof of a binary."""
    for name, markers in CONTEXT_SOURCE_MARKERS.items():
        path = source / name
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0) & 0x400):
            raise ValueError("Candidate context source must be a regular file")
        contents = path.read_text(encoding="utf-8")
        if any(marker not in contents for marker in markers):
            raise ValueError("Candidate context source contract is incomplete")
        if any(marker in contents for marker in CONTEXT_FORBIDDEN_SOURCE_MARKERS.get(name, ())):
            raise ValueError("Candidate context source contains a removed estimate admission gate")


def identity(root: Path = ROOT) -> dict:
    """Hash the named source preparation inputs, never claim a built artifact."""
    inputs = {}
    for name in INPUTS:
        path = root / name
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError(f"Candidate input must be a regular file: {name}")
        inputs[name] = common.normalized_sha256(path)
    payload = {"schema": SCHEMA, "version": VERSION,
               "upstream_revision": UPSTREAM_REVISION,
               "rust_version": RUST_VERSION, "v8_release": V8_RELEASE,
               "inputs": inputs, "published": False,
               "qualification": "source-only; native build and proofs required"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "source_key": hashlib.sha256(encoded).hexdigest()}


def prepare(source: Path, *, root: Path = ROOT) -> dict:
    before = identity(root)
    # The git revision is verified by the shared helper. An archive imported
    # into a new local commit is deliberately not accepted as the official SHA.
    common.prepare(source, root / PATCH, expected_revision=UPSTREAM_REVISION, version=VERSION)
    if identity(root) != before:
        raise ValueError("Candidate preparation inputs changed during application")
    verify_root_roundtrip_source(source)
    verify_context_source(source)
    return before


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity")
    stage = sub.add_parser("prepare")
    stage.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    result = identity() if args.command == "identity" else prepare(args.source)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
