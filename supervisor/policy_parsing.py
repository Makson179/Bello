"""Conservative shell, patch and command-operand parsing without approval decisions.

Runtime dependencies are looked up through ``supervisor.policy`` so existing
imports and monkeypatches keep their original effect. The local imports defer
that lookup until a call; these helpers own no mutable engine state.
"""
from __future__ import annotations

from pathlib import Path

from supervisor.policy_types import ShellKind


SUPPORTED_COMPOSITION_OPERATORS = {"|", "&&"}


SHELL_PUNCTUATION = "|&;()<>"


SHELL_OPERATORS = {"|", "&&", "||", ";", "&"}


SHELL_REDIRECT_OPERATORS = {">", ">>", "<", "<<", "<<<", "<>", ">|", "&>", "2>", "2>>"}


SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}


WINDOWS_SHELL_COMMANDS = {"cmd", "powershell", "pwsh"}


def native_shell_kind() -> ShellKind:
    """Return the command language used for unwrapped native commands.

    Codex uses PowerShell for its native Windows shell surface.  Explicit
    ``cmd.exe /c`` commands are detected separately.  Keeping this decision in
    one small helper also lets non-Windows tests exercise the native branch
    without mutating ``os.name`` (which would confuse ``pathlib``).
    """

    from supervisor import policy as _policy

    return "powershell" if _policy.sys.platform == "win32" else "posix"


def is_windows_shell_kind(shell_kind: ShellKind) -> bool:
    return shell_kind in {"powershell", "cmd"}


def _resolved_shell_kind(shell_kind: ShellKind | None) -> ShellKind:
    from supervisor import policy as _policy

    return shell_kind or _policy.native_shell_kind()


def _lex_shell_command(command: str) -> tuple[list[str] | None, str | None]:
    from supervisor import policy as _policy

    if "\n" in command or "\r" in command:
        return None, "multiline shell command requires supervisor judgment"
    try:
        lexer = _policy.shlex.shlex(command, posix=True, punctuation_chars=_policy.SHELL_PUNCTUATION)
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

    from supervisor import policy as _policy

    if not _policy.is_windows_shell_kind(shell_kind):
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
    from supervisor import policy as _policy

    return _policy._executable_basename(_policy._leading_windows_executable(command)) in _policy.WINDOWS_SHELL_COMMANDS


def windows_shell_wrapper_payload(
    command: str,
) -> tuple[ShellKind, str | None, str | None] | None:
    """Return a conservatively extracted PowerShell/cmd payload.

    A returned tuple always identifies the wrapper.  ``payload`` is ``None``
    when the wrapper shape is ambiguous, allowing callers to fail closed rather
    than falling through to a POSIX parser.
    """

    from supervisor import policy as _policy

    if not _policy.command_is_windows_shell_wrapper(command):
        return None
    leading = _policy._executable_basename(_policy._leading_windows_executable(command))
    shell_kind: _policy.ShellKind = "cmd" if leading == "cmd" else "powershell"
    tokens, problem = _policy.lex_windows_command(command, shell_kind)
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


def _split_command_segments(tokens: list[str]) -> tuple[list[list[str]], list[str], set[str], str | None]:
    from supervisor import policy as _policy

    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    tags: set[str] = set()
    parse_error: str | None = None

    for token in tokens:
        if token in _policy.SHELL_REDIRECT_OPERATORS or any(char in token for char in (">", "<")):
            tags.add("shell_redirection")
            parse_error = parse_error or "shell redirection requires supervisor judgment"
            continue
        if token in {"(", ")"}:
            tags.add("ambiguous_parse")
            parse_error = parse_error or "shell grouping requires supervisor judgment"
            continue
        if token in _policy.SHELL_OPERATORS:
            if token == "&":
                tags.add("background_execution")
                parse_error = parse_error or "background execution requires supervisor judgment"
            elif token not in _policy.SUPPORTED_COMPOSITION_OPERATORS:
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
    from supervisor import policy as _policy

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
        if _policy._looks_like_path_argument(arg, cwd, windows_paths=windows_paths):
            paths.append(arg)
        index += 1
    return paths, explicit_secret_search


def _windows_py_launcher_args(args: list[str]) -> tuple[list[str] | None, str | None]:
    from supervisor import policy as _policy

    remaining = list(args)
    if remaining and _policy.re.fullmatch(r"-3(?:\.\d+)?", remaining[0]):
        remaining = remaining[1:]
    elif remaining and (
        _policy.re.match(r"^-\d", remaining[0])
        or remaining[0].casefold().startswith(("-v:", "--list", "--company", "--tag"))
    ):
        return None, "Python launcher selector requires supervisor judgment"
    return remaining, None


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


def parse_command(
    command: str,
    *,
    shell_kind: ShellKind | None = None,
) -> tuple[list[str] | None, str | None]:
    from supervisor import policy as _policy

    resolved_shell = _policy._resolved_shell_kind(shell_kind)
    if _policy.is_windows_shell_kind(resolved_shell):
        return _policy.lex_windows_command(command, resolved_shell, cross_shell_safe=True)
    try:
        tokens = _policy.shlex.split(command)
    except ValueError as exc:
        return None, f"cannot parse shell command: {exc}"
    if not tokens:
        return None, "empty command"
    shell_meta = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}
    if any(token in shell_meta or "$(" in token or "`" in token for token in tokens):
        return tokens, "shell metacharacters require LLM review"
    return tokens, None


def _shell_payload_from_tokens(tokens: list[str]) -> str | None:
    from supervisor import policy as _policy

    if len(tokens) < 3:
        return None
    executable = _policy.Path(tokens[0]).name.lower()
    if executable not in _policy.SHELL_COMMANDS:
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


def _sed_read_paths(
    args: list[str],
    workspace: Path,
    *,
    windows_paths: bool = False,
) -> tuple[list[str], str | None]:
    from supervisor import policy as _policy

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
        if _policy._looks_like_path_argument(arg, workspace, windows_paths=windows_paths):
            candidates.append(arg)
        index += 1
    return candidates, None


def extract_read_command_paths(
    tokens: list[str],
    workspace: Path,
    *,
    windows_paths: bool = False,
) -> tuple[list[str], str | None]:
    from supervisor import policy as _policy

    if not tokens or tokens[0] not in _policy.READ_FILE_COMMANDS:
        return [], None
    if tokens[0] == "sed":
        return _policy._sed_read_paths(tokens[1:], workspace, windows_paths=windows_paths)
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
        if _policy._looks_like_path_argument(arg, workspace, windows_paths=windows_paths):
            candidates.append(arg)
    return candidates, None


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
