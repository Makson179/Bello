from __future__ import annotations

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


SECRET_FILE_GLOBS = {
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "service-account*.json",
}

SECRET_NAME_PARTS = {
    "secret",
    "credential",
    "password",
    "passwd",
    "token",
    "apikey",
    "api_key",
    "private",
    "vault",
}

SECRET_PATH_PARTS = {
    ".git",
    ".ssh",
    ".aws",
    ".kube",
}

CHEATING_WORKSPACE_PATH_PARTS = {"hidden", "id_private", "private", "grading"}
GRADING_PATH_RISK_TAG = "grading_path"

SECRET_PATH_SUFFIXES = {
    (".config", "gh"),
    (".config", "gcloud"),
    (".docker", "config.json"),
}

READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "List", "Search"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
APPLY_PATCH_TOOLS = {"apply_patch", "ApplyPatch"}
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
SUPPORTED_COMPOSITION_OPERATORS = {"|", "&&"}
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
SHELL_PUNCTUATION = "|&;()<>"
SHELL_OPERATORS = {"|", "&&", "||", ";", "&"}
SHELL_REDIRECT_OPERATORS = {">", ">>", "<", "<<", "<<<", "<>", ">|", "&>", "2>", "2>>"}
NETWORK_COMMANDS = {"curl", "wget"}
SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}
WINDOWS_SHELL_COMMANDS = {"cmd", "powershell", "pwsh"}
ShellKind = Literal["posix", "powershell", "cmd"]

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


class ParsedCommandSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executable: str
    args: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)
    raw_paths: list[str] = Field(default_factory=list)
    resolved_paths: list[str] = Field(default_factory=list)
    read_only: bool = False


class CommandAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    tokens: list[str] = Field(default_factory=list)
    segments: list[ParsedCommandSegment] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    resolved_paths: list[str] = Field(default_factory=list)
    risk_tags: set[str] = Field(default_factory=set)
    parse_error: str | None = None

    def policy_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["risk_tags"] = sorted(self.risk_tags)
        return data


def command_analysis_from_policy_decision(evaluation: PolicyDecision) -> CommandAnalysis | None:
    raw = evaluation.payload.get("command_analysis")
    if isinstance(raw, CommandAnalysis):
        return raw
    if isinstance(raw, dict):
        try:
            return CommandAnalysis.model_validate(raw)
        except ValidationError:
            return None
    return None


def native_shell_kind() -> ShellKind:
    """Return the command language used for unwrapped native commands.

    Codex uses PowerShell for its native Windows shell surface.  Explicit
    ``cmd.exe /c`` commands are detected separately.  Keeping this decision in
    one small helper also lets non-Windows tests exercise the native branch
    without mutating ``os.name`` (which would confuse ``pathlib``).
    """

    return "powershell" if sys.platform == "win32" else "posix"


def is_windows_shell_kind(shell_kind: ShellKind) -> bool:
    return shell_kind in {"powershell", "cmd"}


def _resolved_shell_kind(shell_kind: ShellKind | None) -> ShellKind:
    return shell_kind or native_shell_kind()


def windows_path_syntax_problem(raw: str | os.PathLike[str]) -> str | None:
    """Reject Win32 spellings whose target cannot be established safely.

    In particular, drive-relative paths, device namespaces, alternate data
    streams, reserved DOS devices, and Win32-normalized trailing dots/spaces
    must never become a second spelling that bypasses a protected root check.
    """

    text = os.fspath(raw)
    if not text or text.startswith(("http://", "https://")):
        return None
    if "\x00" in text:
        return "NUL in Windows path"
    normalized = text.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        return "Windows device/extended path is ambiguous"
    drive, tail = ntpath.splitdrive(normalized)
    if drive and not (
        (len(drive) == 2 and drive[0].isalpha() and drive[1] == ":")
        or drive.startswith("\\\\")
    ):
        return "Windows provider or malformed drive path is ambiguous"
    if drive and len(drive) == 2 and drive[1] == ":" and (not tail or not tail.startswith("\\")):
        return "drive-relative Windows path is ambiguous"
    if ":" in tail:
        return "Windows alternate data stream path is ambiguous"
    for part in (part for part in normalized.split("\\") if part):
        if part in {".", ".."} or part == drive:
            continue
        issue = windows_path_component_issue(part)
        if issue is not None:
            return issue
    return None


def _path_for_platform(raw: str | os.PathLike[str], *, windows_paths: bool) -> Path | None:
    text = os.fspath(raw)
    if windows_paths:
        if windows_path_syntax_problem(text) is not None:
            return None
        # On a real Windows host pathlib already implements drive and UNC
        # semantics.  On POSIX, this conversion lets tests exercise relative
        # backslash paths while absolute drive/UNC inputs stay fail-closed.
        if os.name != "nt" and ntpath.isabs(text):
            return None
        if os.name != "nt":
            text = text.replace("\\", "/")
    try:
        return Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def _path_comparison_key(path: Path, *, windows_paths: bool) -> str:
    text = str(path)
    if windows_paths:
        return ntpath.normcase(text.replace("/", "\\")).rstrip("\\")
    return text


def _path_is_within(path: Path, root: Path, *, windows_paths: bool) -> bool:
    if not windows_paths:
        return _is_relative_to(path, root)
    candidate = _path_comparison_key(path, windows_paths=True)
    boundary = _path_comparison_key(root, windows_paths=True)
    if candidate == boundary:
        return True
    return bool(boundary and candidate.startswith(boundary + "\\"))


def path_root_hit(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    roots: Iterable[Path],
    windows_paths: bool = False,
) -> str | None:
    path = _path_for_platform(raw, windows_paths=windows_paths)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            continue
        if _path_is_within(resolved, resolved_root, windows_paths=windows_paths):
            return str(resolved_root)
    return None


def _parts_lower(path: Path) -> list[str]:
    return [part.lower() for part in path.parts]


def is_secret_path(path: Path) -> bool:
    parts = _parts_lower(path)
    name = path.name.lower()
    if any(part in SECRET_PATH_PARTS for part in parts):
        return True
    for suffix in SECRET_PATH_SUFFIXES:
        if len(parts) >= len(suffix) and tuple(parts[-len(suffix) :]) == suffix:
            return True
    if any(fragment in name for fragment in SECRET_NAME_PARTS):
        return True
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in SECRET_FILE_GLOBS)


def is_workspace_cheating_path(workspace: Path, path: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(workspace.resolve()).parts
    except ValueError:
        return False
    return any(part.lower() in CHEATING_WORKSPACE_PATH_PARTS for part in relative_parts)


def is_supervisor_runtime_path(workspace: Path, path: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(workspace.resolve()).parts
    except ValueError:
        return False
    return any(part.lower() == ".supervisor" for part in relative_parts)


def is_protected_path(workspace: Path, path: Path) -> bool:
    return is_secret_path(path) or is_workspace_cheating_path(workspace, path)


def _resolve_outside_candidate(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    windows_paths: bool | None = None,
) -> Path | None:
    # Leading/trailing whitespace is part of a Win32 path spelling.  Trimming
    # it before validation would turn an unsafe alias such as ``"file "``
    # into the different, apparently safe path ``"file"``.
    text = os.fspath(raw).strip("'\"")
    if not text or text.startswith(("http://", "https://")):
        return None
    windows = (sys.platform == "win32") if windows_paths is None else windows_paths
    path = _path_for_platform(text, windows_paths=windows)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _declared_grading_path_hit(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    roots: tuple[Path, ...],
    windows_paths: bool = False,
) -> str | None:
    if not roots:
        return None
    return path_root_hit(raw, cwd=cwd, roots=roots, windows_paths=windows_paths)


def _declared_roots_from_env() -> tuple[Path, ...]:
    raw = os.environ.get("BELLO_DECLARED_GRADING_PATHS", "")
    if not raw:
        return ()
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        if not item.strip():
            continue
        resolved = _resolve_outside_candidate(item, cwd=Path.cwd())
        if resolved is not None:
            roots.append(resolved)
    return tuple(dict.fromkeys(roots))


def normalize_path(
    workspace: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool | None = None,
) -> Path | None:
    windows = (sys.platform == "win32") if windows_paths is None else windows_paths
    return _normalize_path(workspace, workspace, raw, windows_paths=windows)


def _normalize_path(
    workspace: Path,
    cwd: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool,
) -> Path | None:
    raw_text = os.fspath(raw)
    simulated_host_absolute = windows_paths and os.name != "nt" and Path(raw_text).is_absolute()
    if not windows_paths:
        # Preserve the established POSIX contract: command path normalization
        # treats ``~`` lexically here (the shell will expand it later), which
        # lets secret-name checks see components such as ``.ssh``.
        path = Path(raw_text)
    else:
        path = Path(raw_text).expanduser() if simulated_host_absolute else _path_for_platform(raw, windows_paths=True)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        path_exists = path.exists()
    except (OSError, ValueError):
        # Win32 rejects wildcard and otherwise malformed components before a
        # filesystem lookup.  Treat those spellings as ambiguous instead of
        # letting policy evaluation crash; lexical protected-path checks still
        # get a chance to hard-deny names such as ``.super*``.
        return None
    parent = path if path_exists else path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, ValueError):
        try:
            resolved_parent = parent.resolve(strict=False)
        except (OSError, ValueError):
            return None
    resolved = resolved_parent if path_exists else resolved_parent / path.name
    if not _path_is_within(resolved, workspace.resolve(), windows_paths=windows_paths):
        return None
    return resolved


def normalize_path_from_cwd(
    workspace: Path,
    cwd: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool | None = None,
) -> Path | None:
    windows = (sys.platform == "win32") if windows_paths is None else windows_paths
    return _normalize_path(workspace, cwd, raw, windows_paths=windows)


def _workspace_relative(workspace: Path, path: Path, *, windows_paths: bool = False) -> str:
    if not _path_is_within(path, workspace.resolve(), windows_paths=windows_paths):
        return str(path)
    try:
        return path.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        # The POSIX simulation of a case-insensitive Windows path may differ
        # only by case.  It is still contained according to the comparison
        # above; retain the resolved spelling for diagnostics.
        return str(path)


def _command_working_directory(
    workspace: Path,
    cwd: str | None,
    *,
    windows_paths: bool = False,
) -> tuple[Path, set[str], str | None]:
    if cwd is None:
        return workspace.resolve(), set(), None
    resolved = _normalize_path(workspace, workspace, cwd, windows_paths=windows_paths)
    if resolved is None:
        return workspace.resolve(), {"workspace_escape"}, "command working directory escapes workspace or is ambiguous"
    if is_protected_path(workspace, resolved):
        return resolved, {"secret_path"}, None
    return resolved, set(), None


def _lex_shell_command(command: str) -> tuple[list[str] | None, str | None]:
    if "\n" in command or "\r" in command:
        return None, "multiline shell command requires supervisor judgment"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer), None
    except ValueError as exc:
        return None, f"cannot parse shell command: {exc}"


def lex_windows_command(
    command: str,
    shell_kind: ShellKind,
    *,
    cross_shell_safe: bool = False,
) -> tuple[list[str] | None, str | None]:
    """Tokenize the deliberately small Windows command subset we can prove safe.

    This is not an attempted PowerShell or ``cmd.exe`` grammar.  Expansion,
    composition, escaping, script blocks, redirection, and multiline input are
    rejected, which is the security boundary needed for deterministic approval.
    The full supervisor can still judge every rejected command.
    """

    if not is_windows_shell_kind(shell_kind):
        raise ValueError("lex_windows_command requires a Windows shell kind")
    if not command.strip():
        return None, "empty Windows command"
    if "\n" in command or "\r" in command:
        return None, f"multiline {shell_kind} command requires supervisor judgment"

    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    token_started = False
    index = 0
    while index < len(command):
        char = command[index]
        if (shell_kind == "powershell" or cross_shell_safe) and char in {"$", "`"}:
            return None, "PowerShell expansion/escaping requires supervisor judgment"
        if (shell_kind == "cmd" or cross_shell_safe) and char in {"%", "!", "^"}:
            return None, "cmd.exe expansion/escaping requires supervisor judgment"
        if char in {"*", "?", "[", "]", ","}:
            return None, f"{shell_kind} wildcard/argument expansion requires supervisor judgment"

        if quote is not None:
            if char == quote:
                if index + 1 < len(command) and command[index + 1] == quote:
                    return None, f"{shell_kind} doubled-quote escaping requires supervisor judgment"
                quote = None
            else:
                if shell_kind == "cmd" and char == "\\" and index + 1 < len(command) and command[index + 1] == '"':
                    return None, "cmd.exe backslash/quote boundary requires supervisor judgment"
                current.append(char)
            token_started = True
            index += 1
            continue

        if char.isspace():
            if token_started:
                tokens.append("".join(current))
                current = []
                token_started = False
            index += 1
            continue
        if char == '"' or (shell_kind == "powershell" and char == "'"):
            if index + 1 < len(command) and command[index + 1] == char:
                return None, f"{shell_kind} doubled-quote boundary requires supervisor judgment"
            quote = char
            token_started = True
            index += 1
            continue
        if char in "<>":
            return None, f"{shell_kind} redirection requires supervisor judgment"
        if char in "|&;(){}":
            return None, f"{shell_kind} composition/grouping requires supervisor judgment"
        if shell_kind == "powershell" and char in {"@", "#"}:
            return None, "PowerShell splatting/comment syntax requires supervisor judgment"
        current.append(char)
        token_started = True
        index += 1

    if quote is not None:
        return None, f"unterminated {shell_kind} quoted string requires supervisor judgment"
    if token_started:
        tokens.append("".join(current))
    if not tokens or not tokens[0]:
        return None, f"empty {shell_kind} executable requires supervisor judgment"
    return tokens, None


def _leading_windows_executable(command: str) -> str:
    text = command.lstrip()
    if not text:
        return ""
    if text[0] in {'"', "'"}:
        quote = text[0]
        end = text.find(quote, 1)
        return text[1:end] if end >= 0 else text[1:]
    return text.split(None, 1)[0]


def command_is_windows_shell_wrapper(command: str) -> bool:
    return _executable_basename(_leading_windows_executable(command)) in WINDOWS_SHELL_COMMANDS


def windows_shell_wrapper_payload(
    command: str,
) -> tuple[ShellKind, str | None, str | None] | None:
    """Return a conservatively extracted PowerShell/cmd payload.

    A returned tuple always identifies the wrapper.  ``payload`` is ``None``
    when the wrapper shape is ambiguous, allowing callers to fail closed rather
    than falling through to a POSIX parser.
    """

    if not command_is_windows_shell_wrapper(command):
        return None
    leading = _executable_basename(_leading_windows_executable(command))
    shell_kind: ShellKind = "cmd" if leading == "cmd" else "powershell"
    tokens, problem = lex_windows_command(command, shell_kind)
    if tokens is None:
        return shell_kind, None, problem

    if shell_kind == "powershell":
        command_flags = {"-c", "-command"}
        forbidden_flags = {"-e", "-ec", "-enc", "-encodedcommand", "-file"}
        lowered = [token.casefold() for token in tokens]
        if any(token in forbidden_flags for token in lowered[1:]):
            return shell_kind, None, "PowerShell encoded/file command requires supervisor judgment"
        indexes = [index for index, token in enumerate(lowered[1:], start=1) if token in command_flags]
    else:
        lowered = [token.casefold() for token in tokens]
        if "/k" in lowered[1:]:
            return shell_kind, None, "persistent cmd.exe session requires supervisor judgment"
        indexes = [index for index, token in enumerate(lowered[1:], start=1) if token == "/c"]

    if len(indexes) != 1:
        return shell_kind, None, f"{shell_kind} wrapper command flag is missing or ambiguous"
    payload_index = indexes[0] + 1
    if payload_index != len(tokens) - 1 or not tokens[payload_index].strip():
        return shell_kind, None, f"{shell_kind} wrapper payload boundaries are ambiguous"
    return shell_kind, tokens[payload_index], None


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


def _split_command_segments(tokens: list[str]) -> tuple[list[list[str]], list[str], set[str], str | None]:
    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    tags: set[str] = set()
    parse_error: str | None = None

    for token in tokens:
        if token in SHELL_REDIRECT_OPERATORS or any(char in token for char in (">", "<")):
            tags.add("shell_redirection")
            parse_error = parse_error or "shell redirection requires supervisor judgment"
            continue
        if token in {"(", ")"}:
            tags.add("ambiguous_parse")
            parse_error = parse_error or "shell grouping requires supervisor judgment"
            continue
        if token in SHELL_OPERATORS:
            if token == "&":
                tags.add("background_execution")
                parse_error = parse_error or "background execution requires supervisor judgment"
            elif token not in SUPPORTED_COMPOSITION_OPERATORS:
                tags.add("ambiguous_parse")
                parse_error = parse_error or "unsupported shell composition requires supervisor judgment"
            if current:
                segments.append(current)
                current = []
            else:
                tags.add("ambiguous_parse")
                parse_error = parse_error or "empty command segment requires supervisor judgment"
            operators.append(token)
            continue
        current.append(token)

    if current:
        segments.append(current)
    elif operators:
        tags.add("ambiguous_parse")
        parse_error = parse_error or "trailing shell operator requires supervisor judgment"
    return segments, operators, tags, parse_error


def _resolve_segment_paths(
    workspace: Path,
    cwd: Path,
    raw_paths: Iterable[str],
    tags: set[str],
    *,
    windows_paths: bool = False,
) -> list[str]:
    resolved_paths: list[str] = []
    for raw in raw_paths:
        resolved = normalize_path_from_cwd(workspace, cwd, raw, windows_paths=windows_paths)
        if resolved is None:
            tags.add("workspace_escape")
            continue
        if is_protected_path(workspace, resolved):
            tags.add("secret_path")
        resolved_paths.append(_workspace_relative(workspace, resolved, windows_paths=windows_paths))
    return resolved_paths


def _plain_path_args(args: list[str]) -> list[str]:
    return [arg for arg in args if arg not in {"-", "--"} and not arg.startswith("-")]


def _find_paths_and_bounds(args: list[str]) -> tuple[list[str], bool]:
    paths: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("-") or arg in {"(", "!", ")"}:
            break
        paths.append(arg)
        index += 1
    if not paths:
        paths.append(".")
    bounded = False
    for index, arg in enumerate(args):
        if arg == "-maxdepth" and index + 1 < len(args):
            try:
                bounded = int(args[index + 1]) >= 0
            except ValueError:
                bounded = False
            break
    return paths, bounded


def _grep_like_paths(args: list[str], cwd: Path, *, windows_paths: bool = False) -> tuple[list[str], bool]:
    paths: list[str] = []
    pattern_seen = False
    explicit_secret_search = False
    options_with_values = {
        "-A",
        "-B",
        "-C",
        "-e",
        "-f",
        "-g",
        "-m",
        "--after-context",
        "--before-context",
        "--context",
        "--file",
        "--glob",
        "--max-count",
        "--regexp",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--hidden", "--no-ignore"}:
            explicit_secret_search = True
            index += 1
            continue
        if arg in options_with_values:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if not pattern_seen:
            pattern_seen = True
            index += 1
            continue
        if _looks_like_path_argument(arg, cwd, windows_paths=windows_paths):
            paths.append(arg)
        index += 1
    return paths, explicit_secret_search


def _version_report_only(args: list[str]) -> bool:
    return bool(args) and all(arg in VERSION_FLAGS for arg in args)


def _windows_python_executable(executable: str) -> bool:
    return executable == "py" or bool(re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable))


def _windows_py_launcher_args(args: list[str]) -> tuple[list[str] | None, str | None]:
    remaining = list(args)
    if remaining and re.fullmatch(r"-3(?:\.\d+)?", remaining[0]):
        remaining = remaining[1:]
    elif remaining and (
        re.match(r"^-\d", remaining[0])
        or remaining[0].casefold().startswith(("-v:", "--list", "--company", "--tag"))
    ):
        return None, "Python launcher selector requires supervisor judgment"
    return remaining, None


def _windows_python_version_report_only(executable: str, args: list[str]) -> bool:
    if executable != "py":
        return _version_report_only(args)
    remaining, problem = _windows_py_launcher_args(args)
    return problem is None and remaining is not None and _version_report_only(remaining)


def _git_path_args(args: list[str]) -> list[str]:
    if not args:
        return []
    path_args: list[str] = []
    after_separator = False
    for arg in args[1:]:
        if arg == "--":
            after_separator = True
            continue
        if after_separator and not arg.startswith("-"):
            path_args.append(arg)
    return path_args


def _classify_segment(
    segment_tokens: list[str],
    *,
    workspace: Path,
    cwd: Path,
    receives_stdin: bool,
    shell_kind: ShellKind = "posix",
) -> tuple[ParsedCommandSegment, set[str]]:
    raw_executable = segment_tokens[0] if segment_tokens else ""
    executable = raw_executable
    if is_windows_shell_kind(shell_kind) and raw_executable:
        executable = _executable_basename(raw_executable)
        executable = WINDOWS_COMMAND_ALIASES.get(executable, executable)
    args = segment_tokens[1:]
    tags: set[str] = set()
    raw_paths: list[str] = []
    read_only = False

    if not executable:
        tags.add("ambiguous_parse")
    elif executable in NETWORK_COMMANDS | WINDOWS_NETWORK_COMMANDS or any(
        arg.startswith(("http://", "https://")) for arg in args
    ):
        tags.add("network")
        tags.add("external_side_effect")
    elif executable in SHELL_COMMANDS | WINDOWS_SHELL_COMMANDS:
        tags.add("interpreter_execution")
    elif executable in DESTRUCTIVE_COMMANDS | WINDOWS_DESTRUCTIVE_COMMANDS:
        tags.add("destructive")
        tags.add("filesystem_write")
    elif executable in PERMISSION_COMMANDS:
        tags.add("permission_change")
    elif executable in PROCESS_CONTROL_COMMANDS | WINDOWS_PROCESS_CONTROL_COMMANDS:
        tags.add("process_or_service_control")
    elif executable in DEPLOY_COMMANDS or executable in {"npm"} and any(arg in {"publish", "release"} for arg in args):
        tags.add("deploy_publish_release")
        tags.add("external_side_effect")
    elif executable in DEPENDENCY_MUTATION_COMMANDS or executable == "npm" and any(
        arg in {"add", "ci", "install", "link", "remove", "uninstall", "update"} for arg in args
    ):
        tags.add("dependency_mutation")
        tags.add("filesystem_write")
        tags.add("external_side_effect")
    elif executable == "git":
        if _git_read_only(args):
            read_only = True
            raw_paths = _git_path_args(args)
        else:
            tags.add("git_mutation")
            if any(arg in {"fetch", "pull", "push"} for arg in args):
                tags.add("network")
                tags.add("external_side_effect")
    elif executable in {"ls"}:
        read_only = True
        raw_paths = _plain_path_args(args)
    elif executable == "pwd":
        read_only = not args
        if args:
            tags.add("ambiguous_parse")
    elif executable == "find":
        raw_paths, bounded = _find_paths_and_bounds(args)
        read_only = True
        if not bounded:
            tags.add("ambiguous_parse")
    elif executable in {"rg", "grep"}:
        raw_paths, explicit_secret_search = _grep_like_paths(
            args,
            cwd,
            windows_paths=is_windows_shell_kind(shell_kind),
        )
        read_only = True
        if explicit_secret_search:
            tags.add("secret_path")
        if not raw_paths and not receives_stdin:
            tags.add("ambiguous_parse")
    elif executable in READ_FILE_COMMANDS:
        normalized_tokens = [executable, *args] if is_windows_shell_kind(shell_kind) else segment_tokens
        raw_paths, read_problem = extract_read_command_paths(
            normalized_tokens,
            cwd,
            windows_paths=is_windows_shell_kind(shell_kind),
        )
        if read_problem:
            tags.add("filesystem_write")
        if raw_paths or receives_stdin:
            read_only = read_problem is None
        else:
            tags.add("ambiguous_parse")
    elif executable in {"sort", "uniq"}:
        raw_paths = _plain_path_args(args)
        if raw_paths or receives_stdin:
            read_only = True
        else:
            tags.add("ambiguous_parse")
    elif executable in BOUNDED_FILESYSTEM_WRITE_COMMANDS | WINDOWS_WRITE_COMMANDS:
        # Resolve all path args so workspace_escape / GRADING_PATH_RISK_TAG are raised
        # for any path that leaves the workspace or touches grading material.
        raw_paths = _plain_path_args(args)
        tags.add("filesystem_write")
        if not raw_paths:
            tags.add("ambiguous_parse")
    elif executable in VERSION_REPORT_COMMANDS or (
        is_windows_shell_kind(shell_kind) and _windows_python_executable(executable)
    ):
        py_args, py_problem = (
            _windows_py_launcher_args(args)
            if is_windows_shell_kind(shell_kind) and executable == "py"
            else (args, None)
        )
        if py_problem is not None:
            tags.add("ambiguous_parse")
            tags.add("interpreter_execution")
        elif py_args is not None and _version_report_only(py_args):
            read_only = True
        else:
            tags.add("interpreter_execution")
            if executable == "npm":
                tags.add("dependency_mutation")
    else:
        if "=" in executable:
            tags.add("environment_mutation")
        tags.add("unknown_executable")

    resolved_paths = _resolve_segment_paths(
        workspace,
        cwd,
        raw_paths,
        tags,
        windows_paths=is_windows_shell_kind(shell_kind),
    )
    return (
        ParsedCommandSegment(
            executable=executable,
            args=list(args),
            tokens=list(segment_tokens),
            raw_paths=list(raw_paths),
            resolved_paths=resolved_paths,
            read_only=read_only and not (tags & READ_ONLY_BLOCK_RISK_TAGS),
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
    resolved_shell = _resolved_shell_kind(shell_kind)
    windows_paths = is_windows_shell_kind(resolved_shell)
    workspace = workspace.resolve()
    cwd_path, cwd_tags, cwd_problem = _command_working_directory(
        workspace,
        cwd,
        windows_paths=windows_paths,
    )
    if windows_paths:
        tokens, lex_problem = lex_windows_command(command, resolved_shell, cross_shell_safe=True)
        risk_tags = _windows_initial_risk_tags(command, resolved_shell) | cwd_tags
    else:
        tokens, lex_problem = _lex_shell_command(command)
        risk_tags = _initial_risk_tags(command) | cwd_tags
    segments: list[ParsedCommandSegment] = []
    operators: list[str] = []
    parse_error = cwd_problem or lex_problem

    if tokens is None:
        risk_tags.add("ambiguous_parse")
        return CommandAnalysis(
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
        raw_segments, operators, split_tags, split_problem = _split_command_segments(tokens)
        risk_tags |= split_tags
        parse_error = parse_error or split_problem
    for index, raw_segment in enumerate(raw_segments):
        segment, segment_tags = _classify_segment(
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
    return CommandAnalysis(
        command=command,
        cwd=cwd,
        tokens=tokens,
        segments=segments,
        operators=operators,
        resolved_paths=resolved_paths,
        risk_tags=risk_tags,
        parse_error=parse_error,
    )


def extract_paths(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("path", "file_path", "filepath", "cwd", "directory"):
        value = payload.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for key in ("paths", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, str))
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        candidates.extend(extract_paths(tool_input))
    return candidates


def resolve_all_paths(
    workspace: Path,
    raw_paths: Iterable[str],
    *,
    windows_paths: bool | None = None,
) -> tuple[list[Path], str | None]:
    windows = (sys.platform == "win32") if windows_paths is None else windows_paths
    resolved: list[Path] = []
    for raw in raw_paths:
        path = _normalize_path(workspace, workspace, raw, windows_paths=windows)
        if path is None:
            return [], f"path escapes workspace or is ambiguous: {raw}"
        resolved.append(path)
    return resolved, None


def _git_read_only(args: list[str]) -> bool:
    if not args:
        return False
    subcommand = args[0]
    if subcommand not in {"status", "log", "diff"}:
        return False
    destructive_or_network = {"push", "fetch", "pull", "reset", "clean", "checkout", "switch"}
    return not any(arg in destructive_or_network for arg in args)


def parse_command(
    command: str,
    *,
    shell_kind: ShellKind | None = None,
) -> tuple[list[str] | None, str | None]:
    resolved_shell = _resolved_shell_kind(shell_kind)
    if is_windows_shell_kind(resolved_shell):
        return lex_windows_command(command, resolved_shell, cross_shell_safe=True)
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return None, f"cannot parse shell command: {exc}"
    if not tokens:
        return None, "empty command"
    shell_meta = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}
    if any(token in shell_meta or "$(" in token or "`" in token for token in tokens):
        return tokens, "shell metacharacters require LLM review"
    return tokens, None


def auto_allow_block_reason(tags: set[str]) -> str | None:
    blocked = tags & AUTO_ALLOW_BLOCK_RISK_TAGS
    if not blocked:
        return None
    if GRADING_PATH_RISK_TAG in blocked:
        return "protected/grading path requires LLM judgment"
    if "secret_path" in blocked:
        return "secret-pattern read requires LLM judgment"
    if "workspace_escape" in blocked:
        return "path escapes workspace or is ambiguous"
    if "ambiguous_parse" in blocked:
        return "command path analysis is ambiguous"
    return "command risk requires LLM judgment: " + ", ".join(sorted(blocked))


def _shell_payload_from_tokens(tokens: list[str]) -> str | None:
    if len(tokens) < 3:
        return None
    executable = Path(tokens[0]).name.lower()
    if executable not in SHELL_COMMANDS:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if not token.startswith("-") or "c" not in token[1:]:
            continue
        if index + 1 >= len(tokens) or index + 2 != len(tokens):
            return None
        return tokens[index + 1]
    return None


def _strip_pytest_selector(value: str) -> str:
    return value.split("::", 1)[0]


def _resolve_candidate_path(
    raw: str,
    *,
    cwd: Path,
    windows_paths: bool = False,
) -> Path | None:
    text = raw.strip("'\"")
    if not text:
        return None
    path = _path_for_platform(text, windows_paths=windows_paths)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _looks_pathish(value: str) -> bool:
    if not value or value.startswith("-"):
        return False
    return value.startswith(("~", "/", ".")) or "/" in value


def _lower_pathish_parts(value: str) -> set[str]:
    return {part.lower() for part in Path(_strip_pytest_selector(value)).parts}


def extract_apply_patch_paths(command: str) -> list[str] | None:
    if "*** Begin Patch" not in command:
        return None
    paths: list[str] = []
    prefixes = (
        "*** Add File: ",
        "*** Update File: ",
        "*** Delete File: ",
        "*** Move to: ",
    )
    for line in command.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                value = line[len(prefix) :].strip()
                if value:
                    paths.append(value)
    return paths


def _looks_like_path_argument(token: str, workspace: Path, *, windows_paths: bool = False) -> bool:
    if token in {"-", "--"} or token.startswith("-"):
        return False
    if token.startswith(("http://", "https://")):
        return False
    path = _path_for_platform(token, windows_paths=windows_paths)
    if path is None:
        return True
    return (
        path.is_absolute()
        or "/" in token
        or (windows_paths and "\\" in token)
        or "." in token
        or (workspace / path).exists()
    )


def _sed_read_paths(
    args: list[str],
    workspace: Path,
    *,
    windows_paths: bool = False,
) -> tuple[list[str], str | None]:
    if any(arg == "-i" or arg.startswith("-i") or arg == "--in-place" for arg in args):
        return [], "sed in-place edit requires LLM review"
    candidates: list[str] = []
    script_seen = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-e", "--expression"}:
            script_seen = True
            index += 2
            continue
        if arg in {"-f", "--file"}:
            if index + 1 < len(args):
                candidates.append(args[index + 1])
            script_seen = True
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if not script_seen:
            script_seen = True
            index += 1
            continue
        if _looks_like_path_argument(arg, workspace, windows_paths=windows_paths):
            candidates.append(arg)
        index += 1
    return candidates, None


def extract_read_command_paths(
    tokens: list[str],
    workspace: Path,
    *,
    windows_paths: bool = False,
) -> tuple[list[str], str | None]:
    if not tokens or tokens[0] not in READ_FILE_COMMANDS:
        return [], None
    if tokens[0] == "sed":
        return _sed_read_paths(tokens[1:], workspace, windows_paths=windows_paths)
    candidates: list[str] = []
    skip_next = False
    options_with_values = {"-n", "--lines", "-c", "--bytes"}
    for arg in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if _looks_like_path_argument(arg, workspace, windows_paths=windows_paths):
            candidates.append(arg)
    return candidates, None


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
    if not tokens or tokens[0] != "chmod":
        return False
    if "777" in tokens:
        return True
    if "-R" not in tokens and "--recursive" not in tokens:
        return False
    for token in tokens[1:]:
        if token.startswith("-") or token.isdigit():
            continue
        path = normalize_path(workspace, token)
        if path is None:
            return True
        if not any(part in {"build", "dist", ".cache", "node_modules", "__pycache__"} for part in path.parts):
            return True
    return False


def is_recursive_delete_outside(tokens: list[str], workspace: Path) -> bool:
    if not tokens or tokens[0] != "rm":
        return False
    recursive = any("r" in token for token in tokens[1:] if token.startswith("-"))
    if not recursive:
        return False
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        if normalize_path(workspace, token) is None:
            return True
        normalized = normalize_path(workspace, token)
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
    targets = recursive_delete_targets(tokens)
    if not targets:
        return None
    tracked: list[str] = []
    root = workspace.resolve()
    for raw in targets:
        path = normalize_path(root, raw)
        if path is None:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        rel_text = str(rel)
        if _git_path_is_tracked_or_contains_tracked(root, rel_text, is_dir=path.is_dir()):
            tracked.append(rel_text)
    if not tracked:
        return None
    return "recursive delete touches git-tracked path(s): " + ", ".join(tracked[:8])


def _git_path_is_tracked_or_contains_tracked(workspace: Path, rel_path: str, *, is_dir: bool) -> bool:
    env = _isolated_git_query_environment()
    try:
        git = (
            require_trusted_executable("git", cwd=workspace, environ=env, windows=True)
            if sys.platform == "win32"
            else "git"
        )
        if is_dir:
            completed = subprocess.run(
                [git, "-c", "core.fsmonitor=false", "ls-files", "--", rel_path.rstrip("/") + "/"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return bool(completed.stdout.strip())
        completed = subprocess.run(
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
    env = os.environ.copy()
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
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


BELLO_CLI_NAMES = {"bello", "supervisor"}


def _executable_basename(executable: str) -> str:
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _is_env_assignment_token(token: str) -> bool:
    name, sep, _value = token.partition("=")
    return bool(sep and name and (name[0].isalpha() or name[0] == "_") and all(ch.isalnum() or ch == "_" for ch in name))


def _tokens_invoke_bello_cli(tokens: list[str]) -> bool:
    index = 0
    if tokens and _executable_basename(tokens[0]) == "env":
        index = 1
    while index < len(tokens) and _is_env_assignment_token(tokens[index]):
        index += 1
    return index < len(tokens) and _executable_basename(tokens[index]) in BELLO_CLI_NAMES


def _token_segments_invoke_bello_cli(tokens: list[str]) -> bool:
    raw_segments, _operators, _tags, _problem = _split_command_segments(tokens)
    return any(_tokens_invoke_bello_cli(segment) for segment in raw_segments)


def command_invokes_bello_cli(analysis: CommandAnalysis) -> bool:
    if any(_tokens_invoke_bello_cli(segment.tokens) for segment in analysis.segments):
        return True
    shell_payload = _shell_payload_from_tokens(analysis.tokens)
    if shell_payload is not None:
        # ``_shell_payload_from_tokens`` only recognizes POSIX shells
        # (bash/zsh/sh).  Do not reinterpret their payload with the native
        # PowerShell lexer when Bello itself runs on Windows.
        tokens, _problem = parse_command(shell_payload, shell_kind="posix")
        if tokens and _token_segments_invoke_bello_cli(tokens):
            return True
    wrapper = windows_shell_wrapper_payload(analysis.command)
    if wrapper is None:
        return False
    shell_kind, payload, _problem = wrapper
    if payload is None:
        return False
    tokens, _problem = parse_command(payload, shell_kind=shell_kind)
    return bool(tokens and _tokens_invoke_bello_cli(tokens))


def command_mentions_supervisor(command: str) -> bool:
    return "supervisor" in command.lower()


def windows_command_may_invoke_bello(command: str, *, _depth: int = 0) -> bool:
    if _depth > 3:
        return False
    candidate = command
    shell_kind: ShellKind = "powershell"
    wrapper = windows_shell_wrapper_payload(command)
    if wrapper is not None:
        shell_kind, payload, _problem = wrapper
        if payload is not None:
            candidate = payload
        else:
            flag = r"/c" if shell_kind == "cmd" else r"-(?:c|command)"
            match = re.search(rf"(?is)(?:^|\s){flag}\s+(.+)$", command)
            if match:
                candidate = match.group(1).strip()
                if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'\"', "'"}:
                    candidate = candidate[1:-1]
    if candidate != command and command_is_windows_shell_wrapper(candidate):
        if windows_command_may_invoke_bello(candidate, _depth=_depth + 1):
            return True
    for raw_segment in re.split(r"[;&|()]", candidate):
        segment = raw_segment.strip()
        if shell_kind == "cmd" and segment.casefold().startswith("call "):
            segment = segment[5:].lstrip()
        if _executable_basename(_leading_windows_executable(segment)) in BELLO_CLI_NAMES:
            return True
    return False


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

    def evaluate_patch_paths(self, raw_paths: list[str]) -> PolicyDecision:
        return self._evaluate_patch_paths(raw_paths)

    def _evaluate_patch_paths(self, raw_paths: list[str]) -> PolicyDecision:
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
        if any(is_protected_path(self.workspace, path) for path in paths):
            return PolicyDecision.deny("writes to secret-pattern paths are denied")
        if any(is_supervisor_runtime_path(self.workspace, path) for path in paths):
            return PolicyDecision.deny("writes to supervisor runtime/state files are denied")
        return PolicyDecision.allow("workspace patch inside workspace")
