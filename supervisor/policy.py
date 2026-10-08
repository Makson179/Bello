"""Public policy API and ordered decision coordinator.

Parsing, filesystem checks, risk analysis and hard-denial predicates live in
cohesive policy modules. Helpers resolve runtime dependencies through this
module deliberately: a plain re-export alone would stop existing callers'
monkeypatches (including private helper and constant overrides) taking effect.
This is a dependency namespace, not shared engine state; PolicyEngine retains
its workspace, roots and shell configuration and the original decision order.
"""
from __future__ import annotations

import errno
import fnmatch
import ntpath
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from supervisor.filesystem_safety import windows_path_component_issue
from supervisor.executables import require_trusted_executable
from supervisor.schemas import PolicyDecision, PolicyDecisionKind

# Keep legacy imports, including private helpers and dependency modules,
# available here for existing callers and monkeypatch targets.
from supervisor.policy_types import (
    ShellKind,
    ParsedCommandSegment,
    CommandAnalysis,
    command_analysis_from_policy_decision,
)

from supervisor.policy_paths import (
    windows_path_syntax_problem,
    _path_for_platform,
    _path_comparison_key,
    _path_is_within,
    _path_has_root_identity,
    path_root_hit,
    _parts_lower,
    is_secret_path,
    is_workspace_cheating_path,
    is_supervisor_runtime_path,
    is_protected_path,
    is_workspace_control_path,
    _resolve_outside_candidate,
    _is_relative_to,
    _declared_grading_path_hit,
    _declared_roots_from_env,
    normalize_path,
    _normalize_path,
    normalize_path_from_cwd,
    _workspace_relative,
    _command_working_directory,
    extract_paths,
    resolve_all_paths,
    _resolve_candidate_path,
    _looks_pathish,
    _lower_pathish_parts,
    _looks_like_path_argument,
    SECRET_FILE_GLOBS,
    SECRET_NAME_PARTS,
    SECRET_PATH_PARTS,
    CHEATING_WORKSPACE_PATH_PARTS,
    SECRET_PATH_SUFFIXES,
)

from supervisor.policy_parsing import (
    native_shell_kind,
    is_windows_shell_kind,
    _resolved_shell_kind,
    _lex_shell_command,
    lex_windows_command,
    _leading_windows_executable,
    command_is_windows_shell_wrapper,
    windows_shell_wrapper_payload,
    _split_command_segments,
    _plain_path_args,
    _find_paths_and_bounds,
    _grep_like_paths,
    _windows_py_launcher_args,
    _git_path_args,
    parse_command,
    _shell_payload_from_tokens,
    _strip_pytest_selector,
    extract_apply_patch_paths,
    _sed_read_paths,
    extract_read_command_paths,
    _executable_basename,
    _is_env_assignment_token,
    SUPPORTED_COMPOSITION_OPERATORS,
    SHELL_PUNCTUATION,
    SHELL_OPERATORS,
    SHELL_REDIRECT_OPERATORS,
    SHELL_COMMANDS,
    WINDOWS_SHELL_COMMANDS,
)

from supervisor.policy_analysis import (
    _initial_risk_tags,
    _windows_initial_risk_tags,
    _resolve_segment_paths,
    _version_report_only,
    _windows_python_executable,
    _windows_python_version_report_only,
    _classify_segment,
    analyze_command,
    _git_read_only,
    auto_allow_block_reason,
    GRADING_PATH_RISK_TAG,
    READ_ONLY_COMMANDS,
    READ_FILE_COMMANDS,
    VERSION_FLAGS,
    VERSION_REPORT_COMMANDS,
    BOUNDED_FILESYSTEM_WRITE_COMMANDS,
    READ_ONLY_BLOCK_RISK_TAGS,
    AUTO_ALLOW_BLOCK_RISK_TAGS,
    NETWORK_COMMANDS,
    WINDOWS_COMMAND_ALIASES,
    WINDOWS_DESTRUCTIVE_COMMANDS,
    WINDOWS_WRITE_COMMANDS,
    WINDOWS_NETWORK_COMMANDS,
    WINDOWS_PROCESS_CONTROL_COMMANDS,
    DESTRUCTIVE_COMMANDS,
    PERMISSION_COMMANDS,
    PROCESS_CONTROL_COMMANDS,
    DEPLOY_COMMANDS,
    DEPENDENCY_MUTATION_COMMANDS,
)

from supervisor.policy_rules import (
    is_remote_execution_pipeline,
    is_force_push_protected,
    is_broad_chmod,
    is_recursive_delete_outside,
    recursive_delete_targets,
    tracked_delete_problem,
    _git_path_is_tracked_or_contains_tracked,
    _isolated_git_query_environment,
    _tokens_invoke_bello_cli,
    _token_segments_invoke_bello_cli,
    command_invokes_bello_cli,
    command_mentions_supervisor,
    windows_command_may_invoke_bello,
    BELLO_CLI_NAMES,
)

READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "List", "Search"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
APPLY_PATCH_TOOLS = {"apply_patch", "ApplyPatch"}


class PolicyEngine:
    def __init__(
        self,
        workspace: Path,
        *,
        declared_grading_roots: Iterable[str | os.PathLike[str]] | None = None,
        immutable_paths: Iterable[str | os.PathLike[str]] | None = None,
        shell_kind: ShellKind | None = None,
    ):
        self.workspace = workspace.resolve()
        self.shell_kind = _resolved_shell_kind(shell_kind)
        self.windows_paths = is_windows_shell_kind(self.shell_kind)
        roots: list[Path] = []
        for raw in declared_grading_roots or ():
            resolved = _resolve_outside_candidate(raw, cwd=self.workspace)
            if resolved is not None:
                roots.append(resolved)
        roots.extend(_declared_roots_from_env())
        self.declared_grading_roots = tuple(dict.fromkeys(roots))
        immutable: list[Path] = []
        for raw in immutable_paths or ():
            resolved = _resolve_outside_candidate(raw, cwd=self.workspace)
            if resolved is not None:
                immutable.append(resolved)
        self.immutable_paths = tuple(dict.fromkeys(immutable))

    def evaluate(self, payload: dict[str, Any]) -> PolicyDecision:
        command = payload.get("command")
        tool_name = payload.get("tool_name")
        operation = payload.get("operation")
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        cwd_path = (
            _resolve_outside_candidate(cwd, cwd=self.workspace, windows_paths=self.windows_paths)
            if cwd
            else self.workspace
        )

        raw_paths = extract_paths(payload)
        immutable_hit = self._immutable_hit_for_raw_paths(raw_paths, cwd=cwd_path or self.workspace)
        if immutable_hit is not None and (operation == "write" or tool_name in WRITE_TOOLS):
            return PolicyDecision.deny(f"immutable path write denied: {immutable_hit}")
        grading_hit = self._declared_grading_hit_for_raw_paths(raw_paths, cwd=cwd_path or self.workspace)
        if grading_hit is not None:
            return PolicyDecision.deny(f"declared grading/hidden path access denied: {grading_hit}")
        paths, path_problem = resolve_all_paths(
            self.workspace,
            raw_paths,
            windows_paths=self.windows_paths,
        )
        if path_problem and raw_paths:
            return PolicyDecision.route_llm(path_problem)

        if any(is_protected_path(self.workspace, path) for path in paths):
            if operation == "write" or tool_name in WRITE_TOOLS:
                return PolicyDecision.deny("writes to secret-pattern paths are denied")
            return PolicyDecision.route_llm("secret-pattern read requires LLM judgment")

        if any(is_supervisor_runtime_path(self.workspace, path) for path in paths):
            if operation == "write" or tool_name in WRITE_TOOLS:
                return PolicyDecision.deny("writes to supervisor runtime/state files are denied")
            return PolicyDecision.route_llm("supervisor runtime/state read requires LLM judgment")

        if isinstance(tool_name, str) and tool_name in APPLY_PATCH_TOOLS:
            patch_text = command if isinstance(command, str) else payload.get("patch")
            if not isinstance(patch_text, str):
                return PolicyDecision.route_llm("apply_patch input missing patch text")
            return self._evaluate_apply_patch(patch_text)

        if isinstance(command, str):
            return self._evaluate_command(command, paths, cwd=cwd)

        if isinstance(tool_name, str):
            if tool_name in WRITE_TOOLS:
                if any(is_protected_path(self.workspace, path) for path in paths):
                    return PolicyDecision.deny("write to secret-pattern path")
                if paths:
                    return PolicyDecision.allow("workspace write tool inside workspace")
                return PolicyDecision.route_llm("write tool did not provide a workspace path")
            if tool_name in READ_ONLY_TOOLS and not path_problem:
                return PolicyDecision.allow("read-only tool inside workspace")
            return PolicyDecision.route_llm("unknown tool requires LLM judgment")

        if operation == "read" and not path_problem:
            return PolicyDecision.allow("read operation inside workspace")
        if operation == "write" and any(is_supervisor_runtime_path(self.workspace, path) for path in paths):
            return PolicyDecision.deny("writes to supervisor runtime/state files are denied")
        if operation == "write" and any(is_protected_path(self.workspace, path) for path in paths):
            return PolicyDecision.deny("write to secret-pattern path")
        return PolicyDecision.route_llm("unclassified event requires LLM judgment")

    def _declared_grading_hit_for_raw_paths(self, raw_paths: Iterable[str], *, cwd: Path) -> str | None:
        for raw in raw_paths:
            hit = _declared_grading_path_hit(
                raw,
                cwd=cwd,
                roots=self.declared_grading_roots,
                windows_paths=self.windows_paths,
            )
            if hit is not None:
                return hit
        return None

    def _immutable_hit_for_raw_paths(self, raw_paths: Iterable[str], *, cwd: Path) -> str | None:
        for raw in raw_paths:
            hit = _declared_grading_path_hit(
                raw,
                cwd=cwd,
                roots=self.immutable_paths,
                windows_paths=self.windows_paths,
            )
            if hit is not None:
                return hit
        return None

    def _raw_windows_command_path_hit(
        self,
        command: str,
        roots: tuple[Path, ...],
    ) -> str | None:
        """Find literal protected paths even when Windows shell syntax is ambiguous."""

        # Shell grammar and filesystem grammar are independent.  Tests may
        # deliberately exercise the legacy POSIX command corpus on a Windows
        # host, while its interpolated paths are still native ``C:\\...``
        # spellings.  Conversely, explicit PowerShell/cmd policy tests on a
        # POSIX host need the same conservative literal check.
        if sys.platform != "win32" and not self.windows_paths:
            return None
        normalized_command = re.sub(r"\\+", r"\\", command.replace("/", "\\").casefold())
        for root in roots:
            spellings = [str(root)]
            if _path_is_within(root, self.workspace, windows_paths=True):
                try:
                    relative = root.relative_to(self.workspace)
                except ValueError:
                    relative = None
                if relative is not None and str(relative) not in {"", "."}:
                    spellings.append(str(relative))
            for spelling in spellings:
                candidate = re.sub(
                    r"\\+",
                    r"\\",
                    spelling.replace("/", "\\").rstrip("\\").casefold(),
                )
                if not candidate:
                    continue
                if re.search(
                    rf"(?<![\w.\\-]){re.escape(candidate)}(?=$|[\\\s'\";&|()<>{{}}])",
                    normalized_command,
                ):
                    return str(root)
        return None

    def _command_immutable_hit(self, analysis: CommandAnalysis, *, cwd: str | None) -> str | None:
        if not self.immutable_paths:
            return None
        if not self.windows_paths:
            from supervisor.immutable_reads import literal_pinned_read_tokens
            checked = literal_pinned_read_tokens(
                analysis.command,
                cwd=Path(cwd).resolve() if cwd else self.workspace,
                immutable_paths=self.immutable_paths,
                workspace=self.workspace,
            )
            if checked is not None:
                # Only this hard-deny check sees the masked operands. Normal
                # analysis, runtime review and execution retain the original
                # command, including every destructive or unknown segment.
                analysis = analysis.model_copy(update={"command": shlex.join(checked), "tokens": checked})
        raw_hit = self._raw_windows_command_path_hit(
            analysis.command,
            self.immutable_paths,
        )
        if raw_hit is not None:
            return raw_hit
        for immutable in self.immutable_paths:
            immutable_text = str(immutable).rstrip(os.sep) or os.sep
            escaped = re.escape(immutable_text)
            flags = re.IGNORECASE if self.windows_paths else 0
            if re.search(rf"(?<![\w./\\-]){escaped}(?=$|[/\\\s'\";&|()])", analysis.command, flags):
                return str(immutable)
        cwd_path = (
            _resolve_outside_candidate(cwd, cwd=self.workspace, windows_paths=self.windows_paths)
            if cwd
            else self.workspace
        )
        if cwd_path is None:
            cwd_path = self.workspace
        candidates = list(analysis.tokens)
        shell_payload = _shell_payload_from_tokens(analysis.tokens)
        if shell_payload:
            # This helper extracts only bash/zsh/sh ``-c`` payloads.
            nested_tokens, _problem = parse_command(shell_payload, shell_kind="posix")
            if nested_tokens:
                candidates.extend(nested_tokens)
        for token in candidates:
            if token in SHELL_OPERATORS or token in SHELL_REDIRECT_OPERATORS or token in {"(", ")"}:
                continue
            if token.startswith("-") or "=" in token and "/" not in token:
                continue
            token_path = _path_for_platform(token.strip("'\""), windows_paths=self.windows_paths)
            if token_path is None:
                continue
            roots = self.immutable_paths
            if not token_path.is_absolute():
                roots = tuple(
                    root
                    for root in roots
                    if not (root.is_dir() and not _is_relative_to(root, self.workspace))
                )
            hit = _declared_grading_path_hit(
                token,
                cwd=cwd_path,
                roots=roots,
                windows_paths=self.windows_paths,
            )
            if hit is not None:
                return hit
        return None

    def _command_declared_grading_hit(self, command: str, analysis: CommandAnalysis, *, cwd: str | None) -> str | None:
        raw_hit = self._raw_windows_command_path_hit(
            command,
            self.declared_grading_roots,
        )
        if raw_hit is not None:
            return raw_hit
        cwd_path = (
            _resolve_outside_candidate(cwd, cwd=self.workspace, windows_paths=self.windows_paths)
            if cwd
            else self.workspace
        )
        if cwd_path is None:
            cwd_path = self.workspace
        cwd_hit = _declared_grading_path_hit(
            str(cwd_path),
            cwd=self.workspace,
            roots=self.declared_grading_roots,
            windows_paths=self.windows_paths,
        )
        if cwd_hit is not None:
            return cwd_hit
        for token in analysis.tokens:
            if token in SHELL_OPERATORS or token in SHELL_REDIRECT_OPERATORS or token in {"(", ")"}:
                continue
            if token.startswith("-") or "=" in token and "/" not in token:
                continue
            pathish = token.startswith(("~", "/", ".")) or "/" in token or (self.windows_paths and "\\" in token)
            if not pathish:
                continue
            hit = _declared_grading_path_hit(
                token,
                cwd=cwd_path,
                roots=self.declared_grading_roots,
                windows_paths=self.windows_paths,
            )
            if hit is not None:
                return hit
        return None

    def _command_targets_supervisor_runtime(self, analysis: CommandAnalysis, *, cwd: str | None) -> bool:
        cwd_path = (
            _resolve_outside_candidate(cwd, cwd=self.workspace, windows_paths=self.windows_paths)
            if cwd
            else self.workspace
        )
        if cwd_path is None:
            cwd_path = self.workspace
        for token in analysis.tokens:
            if token in SHELL_OPERATORS or token in SHELL_REDIRECT_OPERATORS or token in {"(", ")"}:
                continue
            if token.startswith("-"):
                continue
            pathish = token.startswith(("~", "/", ".")) or "/" in token or (self.windows_paths and "\\" in token)
            if not pathish:
                continue
            if self._references_supervisor_runtime(token, cwd=cwd_path):
                return True
        return False

    def _references_supervisor_runtime(self, raw: str, *, cwd: Path) -> bool:
        resolved = _resolve_candidate_path(raw, cwd=cwd, windows_paths=self.windows_paths)
        if resolved is not None and is_supervisor_runtime_path(self.workspace, resolved):
            return True
        text = raw.strip("'\"")
        parts = re.split(r"[\\/]", text) if self.windows_paths else Path(text).parts
        for part in parts:
            lowered = part.lower()
            if lowered.startswith(".") and fnmatch.fnmatch(".supervisor", lowered):
                return True
        return False

    def _evaluate_command(
        self,
        command: str,
        paths: list[Path],
        *,
        cwd: str | None = None,
        _wrapper_depth: int = 0,
    ) -> PolicyDecision:
        # A native Windows wrapper is only a transport for another command.
        # Re-run the hard-deny checks against a safely delimited payload so
        # `powershell -Command "Set-Content TASK.md ..."` and `cmd /c ...`
        # cannot hide immutable, grading, runtime, or Bello access behind the
        # outer interpreter token.  Ambiguous/encoded wrappers still fall
        # through to normal LLM routing and are never auto-approved.
        if self.windows_paths and _wrapper_depth < 4:
            wrapper = windows_shell_wrapper_payload(command)
            if wrapper is not None:
                nested_shell, nested_command, _wrapper_problem = wrapper
                if nested_command is not None:
                    nested_policy = PolicyEngine(
                        self.workspace,
                        declared_grading_roots=self.declared_grading_roots,
                        immutable_paths=self.immutable_paths,
                        shell_kind=nested_shell,
                    )
                    nested = nested_policy._evaluate_command(
                        nested_command,
                        [],
                        cwd=cwd,
                        _wrapper_depth=_wrapper_depth + 1,
                    )
                    if nested.kind == PolicyDecisionKind.DENY:
                        reason = nested.reason
                        if reason != "commands invoking Bello are denied":
                            reason = f"nested {nested_shell} command denied: {reason}"
                        return PolicyDecision.deny(
                            reason,
                            nested_command=nested_command,
                            nested_shell_kind=nested_shell,
                        )
        analysis = analyze_command(self.workspace, command, cwd, shell_kind=self.shell_kind)
        analysis_payload = analysis.policy_payload()
        payload = {
            "command_analysis": analysis_payload,
            "risk_tags": analysis_payload["risk_tags"],
            "parsed_commands": analysis_payload["segments"],
            "resolved_paths": analysis_payload["resolved_paths"],
        }
        grading_hit = self._command_declared_grading_hit(command, analysis, cwd=cwd)
        if grading_hit is not None:
            analysis.risk_tags.add(GRADING_PATH_RISK_TAG)
            payload["risk_tags"] = sorted(analysis.risk_tags)
            return PolicyDecision.deny(f"declared grading/hidden path access denied: {grading_hit}", **payload)
        immutable_hit = self._command_immutable_hit(analysis, cwd=cwd)
        if immutable_hit is not None:
            return PolicyDecision.deny(f"immutable path access escalation denied: {immutable_hit}", **payload)
        if command_invokes_bello_cli(analysis) or (
            self.windows_paths and windows_command_may_invoke_bello(command)
        ):
            return PolicyDecision.deny("commands invoking Bello are denied", **payload)
        if command_mentions_supervisor(command):
            return PolicyDecision.deny("commands containing supervisor are denied", **payload)
        if self._command_targets_supervisor_runtime(analysis, cwd=cwd):
            return PolicyDecision.deny("supervisor runtime/state files are off-limits", **payload)
        patch_paths = extract_apply_patch_paths(command)
        if patch_paths is not None:
            return self._evaluate_patch_paths(patch_paths)
        if is_remote_execution_pipeline(command):
            return PolicyDecision.deny("remote code execution pipeline denied", **payload)
        tokens, problem = parse_command(command, shell_kind=self.shell_kind)
        if tokens is None:
            return PolicyDecision.route_llm(problem or "unparsed command", **payload)
        policy_tokens = list(tokens)
        if self.windows_paths:
            executable = _executable_basename(policy_tokens[0])
            policy_tokens[0] = WINDOWS_COMMAND_ALIASES.get(executable, executable)
        if is_force_push_protected(policy_tokens):
            return PolicyDecision.deny("force push to protected branch denied", **payload)
        if is_broad_chmod(policy_tokens, self.workspace):
            return PolicyDecision.deny("broad permission change denied", **payload)
        tracked_problem = tracked_delete_problem(policy_tokens, self.workspace)
        if tracked_problem:
            return PolicyDecision.deny(tracked_problem, **payload)
        if is_recursive_delete_outside(policy_tokens, self.workspace):
            return PolicyDecision.deny("recursive deletion outside workspace denied", **payload)
        block_reason = auto_allow_block_reason(analysis.risk_tags)
        if block_reason is not None:
            return PolicyDecision.route_llm(block_reason, **payload)
        if problem:
            return PolicyDecision.route_llm(problem, **payload)

        cmd = policy_tokens[0]
        if cmd == "git" and _git_read_only(policy_tokens[1:]):
            return PolicyDecision.allow("read-only git command", **payload)
        if (
            cmd in {"python", "python3", "node", "pytest", "npm"}
            and any(flag in policy_tokens[1:] for flag in VERSION_FLAGS)
        ) or (
            self.windows_paths
            and _windows_python_executable(cmd)
            and _windows_python_version_report_only(cmd, policy_tokens[1:])
        ):
            return PolicyDecision.allow("version check", **payload)
        if cmd in {"ls", "pwd"}:
            return PolicyDecision.allow("informational shell command", **payload)
        if cmd == "find":
            return PolicyDecision.allow("bounded find inside workspace", **payload)
        if cmd in READ_FILE_COMMANDS:
            raw_paths, read_problem = extract_read_command_paths(
                policy_tokens,
                self.workspace,
                windows_paths=self.windows_paths,
            )
            if read_problem:
                return PolicyDecision.route_llm(read_problem, **payload)
            resolved, path_problem = resolve_all_paths(
                self.workspace,
                raw_paths,
                windows_paths=self.windows_paths,
            )
            if path_problem:
                return PolicyDecision.route_llm(path_problem, **payload)
            if not resolved:
                return PolicyDecision.route_llm("read command path could not be determined", **payload)
            if any(is_protected_path(self.workspace, path) for path in resolved):
                return PolicyDecision.route_llm("secret-pattern read requires LLM judgment", **payload)
            return PolicyDecision.allow("read-only command inside workspace", **payload)
        if cmd in READ_ONLY_COMMANDS and cmd not in VERSION_REPORT_COMMANDS and paths:
            return PolicyDecision.allow("read-only command inside workspace", **payload)
        return PolicyDecision.route_llm("command is not in deterministic allow list", **payload)

    def _evaluate_apply_patch(self, command: str) -> PolicyDecision:
        patch_paths = extract_apply_patch_paths(command)
        if patch_paths is None:
            return PolicyDecision.route_llm("apply_patch input is not a patch")
        return self._evaluate_patch_paths(patch_paths)

    def evaluate_patch_paths(
        self, raw_paths: list[str], *, check_path_heuristics: bool = True,
    ) -> PolicyDecision:
        return self._evaluate_patch_paths(raw_paths, check_path_heuristics=check_path_heuristics)

    def _evaluate_patch_paths(
        self, raw_paths: list[str], *, check_path_heuristics: bool = True,
    ) -> PolicyDecision:
        if not raw_paths:
            return PolicyDecision.route_llm("patch paths could not be determined")
        immutable_hit = self._immutable_hit_for_raw_paths(raw_paths, cwd=self.workspace)
        if immutable_hit is not None:
            return PolicyDecision.deny(f"immutable path write denied: {immutable_hit}")
        grading_hit = self._declared_grading_hit_for_raw_paths(raw_paths, cwd=self.workspace)
        if grading_hit is not None:
            return PolicyDecision.deny(f"declared grading/hidden path access denied: {grading_hit}")
        paths, path_problem = resolve_all_paths(
            self.workspace,
            raw_paths,
            windows_paths=self.windows_paths,
        )
        if path_problem:
            return PolicyDecision.route_llm(path_problem)
        # Resolution, explicit authority and immutable roots remain mandatory.
        # Only project-local name guesses are optional for runtime-off export.
        if check_path_heuristics and any(is_protected_path(self.workspace, path) for path in paths):
            return PolicyDecision.deny("writes to secret-pattern paths are denied")
        control_check = is_supervisor_runtime_path if check_path_heuristics else is_workspace_control_path
        if any(control_check(self.workspace, path) for path in paths):
            return PolicyDecision.deny("writes to supervisor runtime/state files are denied")
        return PolicyDecision.allow("workspace patch inside workspace")
