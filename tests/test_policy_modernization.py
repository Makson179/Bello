"""Compatibility and security boundaries of the extracted policy helpers."""
from __future__ import annotations

import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor import policy
from supervisor.policy import (
    CommandAnalysis,
    ParsedCommandSegment,
    PolicyEngine,
    analyze_command,
    command_analysis_from_policy_decision,
    normalize_path,
    parse_command,
    path_root_hit,
    tracked_delete_problem,
)
from supervisor.schemas import PolicyDecision, PolicyDecisionKind


def test_analysis_contract_survives_public_import_and_serialization() -> None:
    analysis = CommandAnalysis(
        command="cat file.txt",
        segments=[ParsedCommandSegment(executable="cat", args=["file.txt"])],
        risk_tags={"workspace_escape", "secret_path"},
    )
    payload = analysis.policy_payload()
    assert payload["risk_tags"] == ["secret_path", "workspace_escape"]
    assert command_analysis_from_policy_decision(
        PolicyDecision.route_llm("review", command_analysis=analysis),
    ) is analysis
    restored = command_analysis_from_policy_decision(
        PolicyDecision.route_llm("review", command_analysis=payload),
    )
    assert type(restored) is CommandAnalysis
    assert restored == analysis
    assert pickle.loads(pickle.dumps(analysis)) == analysis
    # Old pickles resolve classes at the pre-extraction public module path.
    assert pickle.loads(b"csupervisor.policy\nCommandAnalysis\n.") is CommandAnalysis
    assert pickle.loads(b"csupervisor.policy\nParsedCommandSegment\n.") is ParsedCommandSegment
    assert command_analysis_from_policy_decision(
        PolicyDecision.route_llm("review", command_analysis={"command": "cat", "unknown": True}),
    ) is None


def test_imported_helpers_observe_late_native_shell_override(tmp_path: Path, monkeypatch) -> None:
    # Imported above, before the patch: no stale function dependency snapshots.
    monkeypatch.setattr(policy, "native_shell_kind", lambda: "powershell")
    assert PolicyEngine(tmp_path).shell_kind == "powershell"
    assert parse_command("Get-Content $input")[0] is None
    assert "ambiguous_parse" in analyze_command(tmp_path, "Get-Content $input").risk_tags
    assert analyze_command(tmp_path, "Get-Location").segments[0].executable == "pwd"
    # Explicit POSIX shells continue using the POSIX grammar.
    assert parse_command("cat $input", shell_kind="posix") == (["cat", "$input"], None)


def test_imported_helpers_observe_late_windows_lexer_override(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def lexer(command, shell_kind, *, cross_shell_safe=False):
        calls.append((command, shell_kind, cross_shell_safe))
        return None, "test lexer rejected command"

    monkeypatch.setattr(policy, "lex_windows_command", lexer)
    assert parse_command("pwd", shell_kind="cmd") == (None, "test lexer rejected command")
    result = analyze_command(tmp_path, "pwd", shell_kind="cmd")
    assert result.parse_error == "test lexer rejected command"
    assert result.risk_tags == {"ambiguous_parse"}
    assert calls == [("pwd", "cmd", True), ("pwd", "cmd", True)]


def test_imported_path_helpers_observe_late_normalizer_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(policy, "_normalize_path", lambda *args, **kwargs: None)
    assert normalize_path(tmp_path, "file.txt") is None
    assert policy.normalize_path_from_cwd(tmp_path, tmp_path, "file.txt") is None
    assert policy.resolve_all_paths(tmp_path, ["file.txt"]) == (
        [], "path escapes workspace or is ambiguous: file.txt",
    )


def test_analysis_observes_late_operand_path_override(tmp_path: Path, monkeypatch) -> None:
    engine = PolicyEngine(tmp_path, shell_kind="posix")
    monkeypatch.setattr(policy, "normalize_path_from_cwd", lambda *args, **kwargs: None)
    decision = engine.evaluate({"command": "cat file.txt"})
    assert decision.kind == PolicyDecisionKind.ROUTE_LLM
    assert decision.reason == "path escapes workspace or is ambiguous"
    analysis = command_analysis_from_policy_decision(decision)
    assert analysis is not None
    assert analysis.risk_tags == {"workspace_escape"}
    assert analysis.resolved_paths == []
    assert not analysis.segments[0].read_only


def test_path_authority_observes_late_identity_override(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "protected-root"
    calls = []

    def identity(candidate, authority):
        calls.append((candidate, authority))
        return authority == root

    monkeypatch.setattr(policy, "_path_has_root_identity", identity)
    assert path_root_hit("different-spelling", cwd=tmp_path, roots=(root,)) == str(root)
    decision = PolicyEngine(tmp_path, immutable_paths=(root,), shell_kind="posix").evaluate(
        {"tool_name": "Write", "path": "different-spelling"},
    )
    assert decision.kind == PolicyDecisionKind.DENY
    assert decision.reason == f"immutable path write denied: {root}"
    assert calls == [(tmp_path / "different-spelling", root)] * 2


def test_replaced_secret_constants_reach_nested_path_checks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(policy, "SECRET_FILE_GLOBS", {"*.sensitive"})
    decision = PolicyEngine(tmp_path, shell_kind="posix").evaluate({"command": "cat file.sensitive"})
    assert decision.kind == PolicyDecisionKind.ROUTE_LLM
    assert decision.reason == "secret-pattern read requires LLM judgment"
    analysis = command_analysis_from_policy_decision(decision)
    assert analysis is not None
    assert analysis.risk_tags == {"secret_path"}


def test_replaced_rule_and_parser_constants_reach_analysis(tmp_path: Path, monkeypatch) -> None:
    engine = PolicyEngine(tmp_path, shell_kind="posix")
    monkeypatch.setattr(policy, "_git_read_only", lambda args: False)
    decision = engine.evaluate({"command": "git status"})
    assert decision.kind == PolicyDecisionKind.ROUTE_LLM
    assert decision.reason == "command risk requires LLM judgment: git_mutation"
    monkeypatch.setattr(policy, "SUPPORTED_COMPOSITION_OPERATORS", set())
    analysis = analyze_command(tmp_path, "cat first.txt | wc", shell_kind="posix")
    assert analysis.parse_error == "unsupported shell composition requires supervisor judgment"
    assert "ambiguous_parse" in analysis.risk_tags


def test_tracked_deletion_observes_replaced_runtime_dependencies(tmp_path: Path, monkeypatch) -> None:
    queries = []
    resolutions = []

    def trusted(name, **kwargs):
        resolutions.append((name, kwargs))
        return "trusted-git.exe"

    def run(argv, **kwargs):
        queries.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="tracked.txt\n")

    # Replace facade attributes themselves, rather than mutating the shared
    # subprocess/sys modules. Re-export-only refactors miss these overrides.
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(policy, "require_trusted_executable", trusted)
    monkeypatch.setattr(policy, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setenv("GIT_DIR", "/untrusted")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "untrusted-hook")
    assert tracked_delete_problem(["rm", "-rf", "tracked.txt"], tmp_path) == (
        "recursive delete touches git-tracked path(s): tracked.txt"
    )
    assert len(queries) == len(resolutions) == 1
    argv, kwargs = queries[0]
    assert argv == ["trusted-git.exe", "-c", "core.fsmonitor=false", "ls-files",
                    "--error-unmatch", "--", "tracked.txt"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 3
    assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == policy.os.devnull
    assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert not {"GIT_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"} & kwargs["env"].keys()
    assert resolutions[0] == ("git", {"cwd": tmp_path, "environ": kwargs["env"], "windows": True})


def test_git_query_failure_stays_fail_closed_after_extraction(tmp_path: Path, monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise OSError("repository state unavailable")

    monkeypatch.setattr(policy, "subprocess", SimpleNamespace(run=unavailable))
    result = PolicyEngine(tmp_path, shell_kind="posix").evaluate({"command": "rm -rf tracked.txt"})
    assert result.kind == PolicyDecisionKind.DENY
    assert result.reason == "recursive delete touches git-tracked path(s): tracked.txt"


@pytest.mark.parametrize("operation", ["read", "write"])
def test_path_denial_order_remains_stable(tmp_path: Path, operation: str) -> None:
    restricted = tmp_path / "restricted"
    engine = PolicyEngine(
        tmp_path, immutable_paths=(restricted,), declared_grading_roots=(restricted,), shell_kind="posix",
    )
    result = engine.evaluate({"operation": operation, "path": str(restricted / "file.txt")})
    assert result.kind == PolicyDecisionKind.DENY
    reason = "immutable path write denied" if operation == "write" else "declared grading/hidden path access denied"
    assert result.reason == f"{reason}: {restricted}"


@pytest.mark.parametrize("shell_kind", ["posix", "powershell", "cmd"])
def test_engine_authorities_stay_isolated(tmp_path: Path, shell_kind: str) -> None:
    restricted = tmp_path / "restricted"
    guarded = PolicyEngine(tmp_path, immutable_paths=(restricted,), shell_kind=shell_kind)
    ordinary = PolicyEngine(tmp_path, shell_kind=shell_kind)
    for engine, expected in [(guarded, PolicyDecisionKind.DENY), (ordinary, PolicyDecisionKind.ALLOW),
                             (guarded, PolicyDecisionKind.DENY)]:
        result = engine.evaluate_patch_paths(["restricted/file.txt"], check_path_heuristics=False)
        assert result.kind == expected
        control = engine.evaluate_patch_paths([".codex/bello-run/authority.json"], check_path_heuristics=False)
        assert control.kind == PolicyDecisionKind.DENY
        assert control.reason == "writes to supervisor runtime/state files are denied"
