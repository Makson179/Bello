"""Command risk classification and deterministic auto-allow eligibility.

Runtime dependencies are looked up through ``supervisor.policy`` so existing
imports and monkeypatches keep their original effect. The local imports defer
that lookup until a call; these helpers own no mutable engine state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from supervisor.policy_types import CommandAnalysis, ParsedCommandSegment, ShellKind


GRADING_PATH_RISK_TAG = "grading_path"


READ_ONLY_COMMANDS = {
    "cat",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "node",
    "npm",
    "pwd",
    "pytest",
    "python",
    "python3",
    "rg",
    "sed",
    "tail",
    "wc",
}


READ_FILE_COMMANDS = {"cat", "head", "sed", "tail", "wc"}


VERSION_FLAGS = {"--version", "-V", "version"}


VERSION_REPORT_COMMANDS = {"node", "npm", "pytest", "python", "python3"}


# Bounded filesystem-write commands whose path arguments can be resolved reliably.
BOUNDED_FILESYSTEM_WRITE_COMMANDS = {"mkdir", "touch", "cp", "mv", "ln"}


READ_ONLY_BLOCK_RISK_TAGS = {
    "network",
    "shell_redirection",
    "command_substitution",
    "process_substitution",
    "background_execution",
    "unknown_executable",
    "interpreter_execution",
    "git_mutation",
    "filesystem_write",
    "secret_path",
    "workspace_escape",
    "destructive",
    "permission_change",
    "environment_mutation",
    "dependency_mutation",
    "process_or_service_control",
    "external_side_effect",
    "deploy_publish_release",
    "ambiguous_parse",
    GRADING_PATH_RISK_TAG,
}


AUTO_ALLOW_BLOCK_RISK_TAGS = READ_ONLY_BLOCK_RISK_TAGS - {"interpreter_execution"}


NETWORK_COMMANDS = {"curl", "wget"}


# These names are aliases/built-ins in the native Windows shells.  They are
# mapped to the existing policy vocabulary only after the Windows lexer has
# accepted the command as a single, expansion-free command.  This keeps the
# POSIX allow list and parser completely unchanged.
WINDOWS_COMMAND_ALIASES = {
    "cd": "pwd",
    "chdir": "pwd",
    "dir": "ls",
    "get-childitem": "ls",
    "get-content": "cat",
    "get-location": "pwd",
    "select-string": "grep",
    "type": "cat",
}


WINDOWS_DESTRUCTIVE_COMMANDS = {
    "clear-content",
    "del",
    "erase",
    "remove-item",
}


WINDOWS_WRITE_COMMANDS = {
    "add-content",
    "copy",
    "copy-item",
    "md",
    "mkdir",
    "move",
    "move-item",
    "new-item",
    "out-file",
    "ren",
    "rename",
    "rename-item",
    "set-content",
}


WINDOWS_NETWORK_COMMANDS = {"invoke-restmethod", "invoke-webrequest", "irm", "iwr"}


WINDOWS_PROCESS_CONTROL_COMMANDS = {
    "restart-service",
    "sc",
    "start",
    "start-process",
    "stop-process",
    "stop-service",
    "taskkill",
}


DESTRUCTIVE_COMMANDS = {"rm", "rmdir", "unlink"}


PERMISSION_COMMANDS = {"chmod", "chown", "chgrp", "sudo"}


PROCESS_CONTROL_COMMANDS = {"kill", "killall", "pkill", "service", "systemctl", "supervisorctl"}


DEPLOY_COMMANDS = {"deploy", "publish", "release"}


DEPENDENCY_MUTATION_COMMANDS = {"pip", "pip3", "yarn", "pnpm"}


def _initial_risk_tags(command: str) -> set[str]:
    tags: set[str] = set()
    if "$(" in command or "`" in command:
        tags.add("command_substitution")
    if "<(" in command or ">(" in command:
        tags.add("process_substitution")
    return tags


def _windows_initial_risk_tags(command: str, shell_kind: ShellKind) -> set[str]:
    tags: set[str] = set()
    if "$(" in command:
        tags.add("command_substitution")
    if any(char in command for char in (">", "<")):
        tags.add("shell_redirection")
    if "|" in command or ";" in command or "(" in command or ")" in command:
        tags.add("ambiguous_parse")
    if shell_kind == "powershell" and any(char in command for char in ("$", "`", "@", "#")):
        tags.add("ambiguous_parse")
    if shell_kind == "cmd" and any(char in command for char in ("%", "!", "^")):
        tags.add("ambiguous_parse")
    return tags


def _resolve_segment_paths(
    workspace: Path,
    cwd: Path,
    raw_paths: Iterable[str],
    tags: set[str],
    *,
    windows_paths: bool = False,
) -> list[str]:
    from supervisor import policy as _policy

    resolved_paths: list[str] = []
    for raw in raw_paths:
        resolved = _policy.normalize_path_from_cwd(workspace, cwd, raw, windows_paths=windows_paths)
        if resolved is None:
            tags.add("workspace_escape")
            continue
        if _policy.is_protected_path(workspace, resolved):
            tags.add("secret_path")
        resolved_paths.append(_policy._workspace_relative(workspace, resolved, windows_paths=windows_paths))
    return resolved_paths


def _version_report_only(args: list[str]) -> bool:
    from supervisor import policy as _policy

    return bool(args) and all(arg in _policy.VERSION_FLAGS for arg in args)


def _windows_python_executable(executable: str) -> bool:
    from supervisor import policy as _policy

    return executable == "py" or bool(_policy.re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable))


def _windows_python_version_report_only(executable: str, args: list[str]) -> bool:
    from supervisor import policy as _policy

    if executable != "py":
        return _policy._version_report_only(args)
    remaining, problem = _policy._windows_py_launcher_args(args)
    return problem is None and remaining is not None and _policy._version_report_only(remaining)


def _classify_segment(
    segment_tokens: list[str],
    *,
    workspace: Path,
    cwd: Path,
    receives_stdin: bool,
    shell_kind: ShellKind = "posix",
) -> tuple[ParsedCommandSegment, set[str]]:
    from supervisor import policy as _policy

    raw_executable = segment_tokens[0] if segment_tokens else ""
    executable = raw_executable
    if _policy.is_windows_shell_kind(shell_kind) and raw_executable:
        executable = _policy._executable_basename(raw_executable)
        executable = _policy.WINDOWS_COMMAND_ALIASES.get(executable, executable)
    args = segment_tokens[1:]
    tags: set[str] = set()
    raw_paths: list[str] = []
    read_only = False

    if not executable:
        tags.add("ambiguous_parse")
    elif executable in _policy.NETWORK_COMMANDS | _policy.WINDOWS_NETWORK_COMMANDS or any(
        arg.startswith(("http://", "https://")) for arg in args
    ):
        tags.add("network")
        tags.add("external_side_effect")
    elif executable in _policy.SHELL_COMMANDS | _policy.WINDOWS_SHELL_COMMANDS:
        tags.add("interpreter_execution")
    elif executable in _policy.DESTRUCTIVE_COMMANDS | _policy.WINDOWS_DESTRUCTIVE_COMMANDS:
        tags.add("destructive")
        tags.add("filesystem_write")
    elif executable in _policy.PERMISSION_COMMANDS:
        tags.add("permission_change")
    elif executable in _policy.PROCESS_CONTROL_COMMANDS | _policy.WINDOWS_PROCESS_CONTROL_COMMANDS:
        tags.add("process_or_service_control")
    elif executable in _policy.DEPLOY_COMMANDS or executable in {"npm"} and any(arg in {"publish", "release"} for arg in args):
        tags.add("deploy_publish_release")
        tags.add("external_side_effect")
    elif executable in _policy.DEPENDENCY_MUTATION_COMMANDS or executable == "npm" and any(
        arg in {"add", "ci", "install", "link", "remove", "uninstall", "update"} for arg in args
    ):
        tags.add("dependency_mutation")
        tags.add("filesystem_write")
        tags.add("external_side_effect")
    elif executable == "git":
        if _policy._git_read_only(args):
            read_only = True
            raw_paths = _policy._git_path_args(args)
        else:
            tags.add("git_mutation")
            if any(arg in {"fetch", "pull", "push"} for arg in args):
                tags.add("network")
                tags.add("external_side_effect")
    elif executable in {"ls"}:
        read_only = True
        raw_paths = _policy._plain_path_args(args)
    elif executable == "pwd":
        read_only = not args
        if args:
            tags.add("ambiguous_parse")
    elif executable == "find":
        raw_paths, bounded = _policy._find_paths_and_bounds(args)
        read_only = True
        if not bounded:
            tags.add("ambiguous_parse")
    elif executable in {"rg", "grep"}:
        raw_paths, explicit_secret_search = _policy._grep_like_paths(
            args,
            cwd,
            windows_paths=_policy.is_windows_shell_kind(shell_kind),
        )
        read_only = True
        if explicit_secret_search:
            tags.add("secret_path")
        if not raw_paths and not receives_stdin:
            tags.add("ambiguous_parse")
    elif executable in _policy.READ_FILE_COMMANDS:
        normalized_tokens = [executable, *args] if _policy.is_windows_shell_kind(shell_kind) else segment_tokens
        raw_paths, read_problem = _policy.extract_read_command_paths(
            normalized_tokens,
            cwd,
            windows_paths=_policy.is_windows_shell_kind(shell_kind),
        )
        if read_problem:
            tags.add("filesystem_write")
        if raw_paths or receives_stdin:
            read_only = read_problem is None
        else:
            tags.add("ambiguous_parse")
    elif executable in {"sort", "uniq"}:
        raw_paths = _policy._plain_path_args(args)
        if raw_paths or receives_stdin:
            read_only = True
        else:
            tags.add("ambiguous_parse")
    elif executable in _policy.BOUNDED_FILESYSTEM_WRITE_COMMANDS | _policy.WINDOWS_WRITE_COMMANDS:
        # Resolve all path args so workspace_escape / GRADING_PATH_RISK_TAG are raised
        # for any path that leaves the workspace or touches grading material.
        raw_paths = _policy._plain_path_args(args)
        tags.add("filesystem_write")
        if not raw_paths:
            tags.add("ambiguous_parse")
    elif executable in _policy.VERSION_REPORT_COMMANDS or (
        _policy.is_windows_shell_kind(shell_kind) and _policy._windows_python_executable(executable)
    ):
        py_args, py_problem = (
            _policy._windows_py_launcher_args(args)
            if _policy.is_windows_shell_kind(shell_kind) and executable == "py"
            else (args, None)
        )
        if py_problem is not None:
            tags.add("ambiguous_parse")
            tags.add("interpreter_execution")
        elif py_args is not None and _policy._version_report_only(py_args):
            read_only = True
        else:
            tags.add("interpreter_execution")
            if executable == "npm":
                tags.add("dependency_mutation")
    else:
        if "=" in executable:
            tags.add("environment_mutation")
        tags.add("unknown_executable")

    resolved_paths = _policy._resolve_segment_paths(
        workspace,
        cwd,
        raw_paths,
        tags,
        windows_paths=_policy.is_windows_shell_kind(shell_kind),
    )
    return (
        _policy.ParsedCommandSegment(
            executable=executable,
            args=list(args),
            tokens=list(segment_tokens),
            raw_paths=list(raw_paths),
            resolved_paths=resolved_paths,
            read_only=read_only and not (tags & _policy.READ_ONLY_BLOCK_RISK_TAGS),
        ),
        tags,
    )


def analyze_command(
    workspace: Path,
    command: str,
    cwd: str | None = None,
    *,
    shell_kind: ShellKind | None = None,
) -> CommandAnalysis:
    from supervisor import policy as _policy

    resolved_shell = _policy._resolved_shell_kind(shell_kind)
    windows_paths = _policy.is_windows_shell_kind(resolved_shell)
    workspace = workspace.resolve()
    cwd_path, cwd_tags, cwd_problem = _policy._command_working_directory(
        workspace,
        cwd,
        windows_paths=windows_paths,
    )
    if windows_paths:
        tokens, lex_problem = _policy.lex_windows_command(command, resolved_shell, cross_shell_safe=True)
        risk_tags = _policy._windows_initial_risk_tags(command, resolved_shell) | cwd_tags
    else:
        tokens, lex_problem = _policy._lex_shell_command(command)
        risk_tags = _policy._initial_risk_tags(command) | cwd_tags
    segments: list[_policy.ParsedCommandSegment] = []
    operators: list[str] = []
    parse_error = cwd_problem or lex_problem

    if tokens is None:
        risk_tags.add("ambiguous_parse")
        return _policy.CommandAnalysis(
            command=command,
            cwd=cwd,
            tokens=[],
            segments=[],
            operators=[],
            resolved_paths=[],
            risk_tags=risk_tags,
            parse_error=parse_error,
        )

    if windows_paths:
        raw_segments = [tokens]
    else:
        raw_segments, operators, split_tags, split_problem = _policy._split_command_segments(tokens)
        risk_tags |= split_tags
        parse_error = parse_error or split_problem
    for index, raw_segment in enumerate(raw_segments):
        segment, segment_tags = _policy._classify_segment(
            raw_segment,
            workspace=workspace,
            cwd=cwd_path,
            receives_stdin=index > 0 and operators[index - 1] == "|",
            shell_kind=resolved_shell,
        )
        segments.append(segment)
        risk_tags |= segment_tags

    resolved_paths: list[str] = []
    for segment in segments:
        resolved_paths.extend(segment.resolved_paths)
    return _policy.CommandAnalysis(
        command=command,
        cwd=cwd,
        tokens=tokens,
        segments=segments,
        operators=operators,
        resolved_paths=resolved_paths,
        risk_tags=risk_tags,
        parse_error=parse_error,
    )


def _git_read_only(args: list[str]) -> bool:
    if not args:
        return False
    subcommand = args[0]
    if subcommand not in {"status", "log", "diff"}:
        return False
    destructive_or_network = {"push", "fetch", "pull", "reset", "clean", "checkout", "switch"}
    return not any(arg in destructive_or_network for arg in args)


def auto_allow_block_reason(tags: set[str]) -> str | None:
    from supervisor import policy as _policy

    blocked = tags & _policy.AUTO_ALLOW_BLOCK_RISK_TAGS
    if not blocked:
        return None
    if _policy.GRADING_PATH_RISK_TAG in blocked:
        return "protected/grading path requires LLM judgment"
    if "secret_path" in blocked:
        return "secret-pattern read requires LLM judgment"
    if "workspace_escape" in blocked:
        return "path escapes workspace or is ambiguous"
    if "ambiguous_parse" in blocked:
        return "command path analysis is ambiguous"
    return "command risk requires LLM judgment: " + ", ".join(sorted(blocked))
