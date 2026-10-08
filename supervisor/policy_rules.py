"""Hard-denial command predicates and isolated Git tracking queries.

Runtime dependencies are looked up through ``supervisor.policy`` so existing
imports and monkeypatches keep their original effect. The local imports defer
that lookup until a call; these helpers own no mutable engine state.
"""
from __future__ import annotations

from pathlib import Path

from supervisor.policy_types import CommandAnalysis, ShellKind


def is_remote_execution_pipeline(command: str) -> bool:
    lowered = command.lower()
    download = ("curl " in lowered or lowered.startswith("curl ") or "wget " in lowered or lowered.startswith("wget "))
    shell = "| bash" in lowered or "| sh" in lowered or "| zsh" in lowered
    return download and shell


def is_force_push_protected(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git" or "push" not in tokens:
        return False
    if "--force" not in tokens and "-f" not in tokens and "--force-with-lease" not in tokens:
        return False
    protected = {"main", "master", "prod", "production"}
    for token in tokens:
        if token in protected or token.startswith("release/"):
            return True
    return False


def is_broad_chmod(tokens: list[str], workspace: Path) -> bool:
    from supervisor import policy as _policy

    if not tokens or tokens[0] != "chmod":
        return False
    if "777" in tokens:
        return True
    if "-R" not in tokens and "--recursive" not in tokens:
        return False
    for token in tokens[1:]:
        if token.startswith("-") or token.isdigit():
            continue
        path = _policy.normalize_path(workspace, token)
        if path is None:
            return True
        if not any(part in {"build", "dist", ".cache", "node_modules", "__pycache__"} for part in path.parts):
            return True
    return False


def is_recursive_delete_outside(tokens: list[str], workspace: Path) -> bool:
    from supervisor import policy as _policy

    if not tokens or tokens[0] != "rm":
        return False
    recursive = any("r" in token for token in tokens[1:] if token.startswith("-"))
    if not recursive:
        return False
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        if _policy.normalize_path(workspace, token) is None:
            return True
        normalized = _policy.normalize_path(workspace, token)
        if normalized is not None and normalized == workspace.resolve().parent:
            return True
    return False


def recursive_delete_targets(tokens: list[str]) -> list[str]:
    if not tokens or tokens[0] != "rm":
        return []
    recursive = any("r" in token for token in tokens[1:] if token.startswith("-"))
    force = any("f" in token for token in tokens[1:] if token.startswith("-"))
    if not recursive and not force:
        return []
    return [token for token in tokens[1:] if token and not token.startswith("-")]


def tracked_delete_problem(tokens: list[str], workspace: Path) -> str | None:
    from supervisor import policy as _policy

    targets = _policy.recursive_delete_targets(tokens)
    if not targets:
        return None
    tracked: list[str] = []
    root = workspace.resolve()
    for raw in targets:
        path = _policy.normalize_path(root, raw)
        if path is None:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        rel_text = str(rel)
        if _policy._git_path_is_tracked_or_contains_tracked(root, rel_text, is_dir=path.is_dir()):
            tracked.append(rel_text)
    if not tracked:
        return None
    return "recursive delete touches git-tracked path(s): " + ", ".join(tracked[:8])


def _git_path_is_tracked_or_contains_tracked(workspace: Path, rel_path: str, *, is_dir: bool) -> bool:
    from supervisor import policy as _policy

    env = _policy._isolated_git_query_environment()
    try:
        git = (
            _policy.require_trusted_executable("git", cwd=workspace, environ=env, windows=True)
            if _policy.sys.platform == "win32"
            else "git"
        )
        if is_dir:
            completed = _policy.subprocess.run(
                [git, "-c", "core.fsmonitor=false", "ls-files", "--", rel_path.rstrip("/") + "/"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return bool(completed.stdout.strip())
        completed = _policy.subprocess.run(
            [git, "-c", "core.fsmonitor=false", "ls-files", "--error-unmatch", "--", rel_path],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except Exception:
        # Failure to establish trusted repository state must not turn a
        # recursive deletion into an auto-approved command.
        return True


def _isolated_git_query_environment() -> dict[str, str]:
    from supervisor import policy as _policy

    env = _policy.os.environ.copy()
    blocked = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for key in list(env):
        if key in blocked or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = _policy.os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


BELLO_CLI_NAMES = {"bello", "supervisor"}


def _tokens_invoke_bello_cli(tokens: list[str]) -> bool:
    from supervisor import policy as _policy

    index = 0
    if tokens and _policy._executable_basename(tokens[0]) == "env":
        index = 1
    while index < len(tokens) and _policy._is_env_assignment_token(tokens[index]):
        index += 1
    return index < len(tokens) and _policy._executable_basename(tokens[index]) in _policy.BELLO_CLI_NAMES


def _token_segments_invoke_bello_cli(tokens: list[str]) -> bool:
    from supervisor import policy as _policy

    raw_segments, _operators, _tags, _problem = _policy._split_command_segments(tokens)
    return any(_policy._tokens_invoke_bello_cli(segment) for segment in raw_segments)


def command_invokes_bello_cli(analysis: CommandAnalysis) -> bool:
    from supervisor import policy as _policy

    if any(_policy._tokens_invoke_bello_cli(segment.tokens) for segment in analysis.segments):
        return True
    shell_payload = _policy._shell_payload_from_tokens(analysis.tokens)
    if shell_payload is not None:
        # ``_shell_payload_from_tokens`` only recognizes POSIX shells
        # (bash/zsh/sh).  Do not reinterpret their payload with the native
        # PowerShell lexer when Bello itself runs on Windows.
        tokens, _problem = _policy.parse_command(shell_payload, shell_kind="posix")
        if tokens and _policy._token_segments_invoke_bello_cli(tokens):
            return True
    wrapper = _policy.windows_shell_wrapper_payload(analysis.command)
    if wrapper is None:
        return False
    shell_kind, payload, _problem = wrapper
    if payload is None:
        return False
    tokens, _problem = _policy.parse_command(payload, shell_kind=shell_kind)
    return bool(tokens and _policy._tokens_invoke_bello_cli(tokens))


def command_mentions_supervisor(command: str) -> bool:
    return "supervisor" in command.lower()


def windows_command_may_invoke_bello(command: str, *, _depth: int = 0) -> bool:
    from supervisor import policy as _policy

    if _depth > 3:
        return False
    candidate = command
    shell_kind: _policy.ShellKind = "powershell"
    wrapper = _policy.windows_shell_wrapper_payload(command)
    if wrapper is not None:
        shell_kind, payload, _problem = wrapper
        if payload is not None:
            candidate = payload
        else:
            flag = r"/c" if shell_kind == "cmd" else r"-(?:c|command)"
            match = _policy.re.search(rf"(?is)(?:^|\s){flag}\s+(.+)$", command)
            if match:
                candidate = match.group(1).strip()
                if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'\"', "'"}:
                    candidate = candidate[1:-1]
    if candidate != command and _policy.command_is_windows_shell_wrapper(candidate):
        if _policy.windows_command_may_invoke_bello(candidate, _depth=_depth + 1):
            return True
    for raw_segment in _policy.re.split(r"[;&|()]", candidate):
        segment = raw_segment.strip()
        if shell_kind == "cmd" and segment.casefold().startswith("call "):
            segment = segment[5:].lstrip()
        if _policy._executable_basename(_policy._leading_windows_executable(segment)) in _policy.BELLO_CLI_NAMES:
            return True
    return False
