"""Controller command parsing; compatibility exports live in controller."""
from __future__ import annotations

import re

from . import compat


_RESTART_SHELL_NAMES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})


_LITERAL_POWERSHELL_PYTHONPATH_PREFIX = re.compile(
    r"\A\s*\$env:PYTHONPATH\s*=\s*'(?:(?:'')|[^'\r\n])*'\s*;\s*(?P<command>[^\r\n]+?)\s*\Z",
    re.IGNORECASE,
)


_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER = re.compile(
    r'\A(?P<prefix>.*?)"(?P<payload>\$env:PYTHONPATH[^"\r\n]*)"\s*\Z',
    re.IGNORECASE,
)


_SPLICE_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER = re.compile(
    r'\A(?P<prefix>.*?)\'\$env:PYTHONPATH=\'"(?P<tail>\'[^"\r\n]*)"\s*\Z',
    re.IGNORECASE,
)


def _literal_powershell_file_invocation(command: str) -> tuple[list[str], str] | None:
    """Extract a simple literal ``powershell -File script.ps1`` invocation.

    The approval parser deliberately leaves every ``-File`` invocation for
    supervisor judgment.  Runtime evidence classification has a narrower job:
    after Codex has already run a command, recognize a literal script execution
    without treating PowerShell expansion or composition as evidence.  Keep the
    two paths separate so this recognition cannot widen auto-approval policy.
    """

    tokens, problem = compat.lex_windows_command(command, "powershell")
    if tokens is None or problem or compat._executable_basename(tokens[0]) not in {"powershell", "pwsh"}:
        return None
    lowered = [token.casefold() for token in tokens]
    file_indexes = [index for index, token in enumerate(lowered[1:], start=1) if token == "-file"]
    if len(file_indexes) != 1:
        return None
    file_index = file_indexes[0]
    script_index = file_index + 1
    if script_index >= len(tokens):
        return None

    # Accept only the small host-option subset needed for deterministic,
    # non-interactive script execution.  Everything after -File belongs to the
    # script and remains literal because lex_windows_command already rejected
    # expansion, escaping, redirection, and composition.
    switches = {"-nologo", "-noprofile", "-noninteractive", "-mta", "-sta"}
    value_options = {"-executionpolicy", "-inputformat", "-outputformat", "-version", "-windowstyle"}
    index = 1
    while index < file_index:
        option = lowered[index]
        if option in switches:
            index += 1
            continue
        if option in value_options and index + 1 < file_index:
            index += 2
            continue
        return None

    script = tokens[script_index]
    if not script or script.startswith("-") or not script.casefold().endswith(".ps1"):
        return None
    return [compat._executable_basename(script), *tokens[script_index + 1 :]], script


def _literal_powershell_pythonpath_payload(payload: str) -> tuple[list[str], str] | None:
    """Recognize one literal PYTHONPATH assignment followed by one command.

    This is runtime-evidence parsing, not approval parsing.  PowerShell env
    assignments require ``;`` composition, so the approval lexer correctly
    leaves them for supervisor judgment.  Once the command has executed, we
    can safely classify this one narrow form by removing only a single-quoted
    literal PYTHONPATH prefix and passing the entire remainder back through the
    existing fail-closed Windows lexer.
    """

    match = compat._LITERAL_POWERSHELL_PYTHONPATH_PREFIX.fullmatch(payload)
    if match is None:
        return None
    command = match.group("command")
    tokens, problem = compat.lex_windows_command(command, "powershell", cross_shell_safe=True)
    if tokens is None or problem:
        return None
    normalized = list(tokens)
    normalized[0] = compat._executable_basename(normalized[0])
    # The production failure was specifically ``python -m pytest``.  Keeping
    # this exception on that exact action avoids exposing unrelated Python or
    # tool classifiers through a new env-prefix surface.
    if normalized[0] != "py" and compat.re.fullmatch(r"python(?:3(?:\.\d+)?)?", normalized[0]) is None:
        return None
    python_action = compat._windows_python_action(normalized)
    if python_action is None or python_action[:2] != ("module", "pytest"):
        return None
    return normalized, command


def _literal_powershell_pythonpath_invocation(command: str) -> tuple[list[str], str] | None:
    """Extract the narrow PYTHONPATH form from a PowerShell ``-Command`` wrapper."""

    if "\n" in command or "\r" in command:
        return None

    match = compat._QUOTED_POWERSHELL_PYTHONPATH_WRAPPER.fullmatch(command)
    if match is not None:
        payload = match.group("payload")
    else:
        # Codex's Windows command renderer can represent a single quote inside
        # the payload with a POSIX-style quote splice, for example:
        #   -Command '$env:PYTHONPATH='"'C:\deps;src'; python -m pytest -q"
        # Recognize that exact boundary without asking POSIX shlex to interpret
        # arbitrary PowerShell syntax; the two grammars disagree on quote
        # termination and can otherwise hide outer-shell composition.
        match = compat._SPLICE_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER.fullmatch(command)
        if match is None:
            return None
        payload = f"$env:PYTHONPATH={match.group('tail')}"

    marker = "__bello_literal_pythonpath_payload__"
    sanitized_wrapper = f'{match.group("prefix")}\"{marker}\"'
    tokens, problem = compat.lex_windows_command(sanitized_wrapper, "powershell", cross_shell_safe=True)
    if tokens is None or problem:
        return None
    if not tokens or compat._executable_basename(tokens[0]) not in {"powershell", "pwsh"}:
        return None

    lowered = [token.casefold() for token in tokens]
    command_indexes = [
        index for index, token in enumerate(lowered[1:], start=1) if token in {"-c", "-command"}
    ]
    if len(command_indexes) != 1:
        return None
    command_index = command_indexes[0]
    payload_index = command_index + 1
    if payload_index != len(tokens) - 1 or tokens[payload_index] != marker:
        return None

    switches = {"-nologo", "-noprofile", "-noninteractive", "-mta", "-sta"}
    value_options = {"-executionpolicy", "-inputformat", "-outputformat", "-version", "-windowstyle"}
    index = 1
    while index < command_index:
        option = lowered[index]
        if option in switches:
            index += 1
            continue
        if option in value_options and index + 1 < command_index:
            index += 2
            continue
        return None

    return compat._literal_powershell_pythonpath_payload(payload)


def _windows_classification_tokens(command: str) -> tuple[bool, list[str] | None, str | None]:
    """Return normalized tokens for a native/wrapped Windows command.

    The boolean distinguishes "not a Windows command surface" from "a Windows
    surface whose syntax is ambiguous".  Callers must treat the latter as
    unclassified, never fall through to POSIX ``shlex`` or regex matching.
    """

    current = command
    for _ in range(6):
        wrapper = compat.windows_shell_wrapper_payload(current)
        if wrapper is None:
            break
        shell_kind, payload, _problem = wrapper
        if payload is None:
            if shell_kind == "powershell":
                pythonpath_invocation = compat._literal_powershell_pythonpath_invocation(current)
                if pythonpath_invocation is not None:
                    tokens, command_payload = pythonpath_invocation
                    return True, tokens, command_payload
                file_invocation = compat._literal_powershell_file_invocation(current)
                if file_invocation is not None:
                    tokens, script = file_invocation
                    return True, tokens, script
            return True, None, None
        if compat.command_is_windows_shell_wrapper(payload):
            current = payload
            continue
        tokens, problem = compat.lex_windows_command(payload, shell_kind)
        if tokens is None or problem:
            return True, None, payload
        normalized = list(tokens)
        normalized[0] = compat._executable_basename(normalized[0])
        return True, normalized, payload
    else:
        return True, None, None
    if compat.command_is_windows_shell_wrapper(command):
        return True, None, None
    shell_kind = compat.native_shell_kind()
    if shell_kind == "posix":
        return False, None, None
    tokens, problem = compat.lex_windows_command(command, shell_kind, cross_shell_safe=True)
    if tokens is None or problem:
        if shell_kind == "powershell":
            pythonpath_invocation = compat._literal_powershell_pythonpath_payload(command)
            if pythonpath_invocation is not None:
                normalized, command_payload = pythonpath_invocation
                return True, normalized, command_payload
        return True, None, command
    normalized = list(tokens)
    normalized[0] = compat._executable_basename(normalized[0])
    return True, normalized, command


def _windows_tokens_are_git_inspection(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git" or len(tokens) < 2:
        return False
    subcommand = tokens[1].casefold()
    args = [token.casefold() for token in tokens[2:]]
    if subcommand == "branch":
        # Creating, copying, renaming, or deleting a branch is mutation.  The
        # no-argument/options-only forms are the subset we can prove to be an
        # inspection without implementing Git's full option grammar.
        return not any(not arg.startswith("-") for arg in args)
    if subcommand == "remote":
        return not args or args == ["-v"] or (args[0] == "get-url" and len(args) == 2)
    return subcommand in {
        "diff",
        "for-each-ref",
        "log",
        "rev-parse",
        "show",
        "status",
    }


def _windows_effective_tool_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) > 1 and tokens[0] == "npx" and not tokens[1].startswith("-"):
        return [compat._executable_basename(tokens[1]), *tokens[2:]]
    return tokens


def _windows_python_args(tokens: list[str]) -> list[str] | None:
    if not tokens:
        return None
    executable = tokens[0]
    if executable == "py":
        args = list(tokens[1:])
        if args and compat.re.fullmatch(r"-3(?:\.\d+)?", args[0]):
            args = args[1:]
        elif args and (
            compat.re.match(r"^-\d", args[0])
            or args[0].casefold().startswith(("-v:", "--list", "--company", "--tag"))
        ):
            return None
        return args
    if compat.re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        return list(tokens[1:])
    return None


def _windows_python_action(tokens: list[str]) -> tuple[str, str, list[str]] | None:
    """Return Python's first executable action without scanning later argv.

    ``-c code -m pytest`` runs ``code`` and merely passes ``-m pytest`` to that
    code.  Looking for ``-m`` anywhere therefore turns harmless output from a
    different action into false test evidence.  Parse only the small, explicit
    interpreter-option subset that may precede Python's mutually exclusive
    ``-c``/``-m``/script action.
    """

    args = compat._windows_python_args(tokens)
    if args is None:
        return None
    no_value_options = {
        "-b",
        "-bb",
        "-B",
        "-d",
        "-E",
        "-i",
        "-I",
        "-O",
        "-OO",
        "-P",
        "-q",
        "-R",
        "-s",
        "-S",
        "-u",
        "-v",
        "-x",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in no_value_options:
            index += 1
            continue
        if arg in {"-W", "-X", "--check-hash-based-pycs"}:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if (arg.startswith("-W") or arg.startswith("-X")) and len(arg) > 2:
            index += 1
            continue
        if arg in {"-c", "-m"}:
            if index + 1 >= len(args) or not args[index + 1]:
                return None
            return ("command" if arg == "-c" else "module"), args[index + 1], args[index + 2 :]
        if arg == "--":
            if index + 1 >= len(args) or not args[index + 1]:
                return None
            return "script", args[index + 1], args[index + 2 :]
        if arg == "-":
            return "script", arg, args[index + 1 :]
        if arg.startswith("-"):
            return None
        return "script", arg, args[index + 1 :]
    return None


_PYTEST_NO_RUN_OPTIONS = frozenset(
    {
        "--cache-show",
        "--co",
        "--collect-only",
        "--fixtures",
        "--fixtures-per-test",
        "--funcargs",
        "--help",
        "--markers",
        "--setup-only",
        "--setup-plan",
        "--version",
    }
)


def _pytest_args_request_no_test_execution(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if (
            arg.startswith("-h")
            or compat.re.fullmatch(r"-[qvxslf]+h.*", arg)
            or compat.re.fullmatch(r"-(?:h|V)+", arg)
        ):
            return True
        if not arg.startswith("--"):
            continue
        option = arg.casefold().partition("=")[0]
        if option in compat._PYTEST_NO_RUN_OPTIONS:
            return True
    return False


def _windows_tokens_are_static_validation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tokens = compat._windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable == "git":
        return bool(args and args[0] == "diff" and "--check" in args)
    if executable in {"node", "nodejs"}:
        return bool(args and args[0] in {"-c", "--check"})
    if executable in {"eslint"}:
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        command_args = args[1:] if args[:1] == ["run"] else args
        return bool(
            command_args
            and (
                command_args[0] == "lint"
                or command_args[0].startswith("lint:")
                or command_args[0].startswith(("type-check", "typecheck"))
            )
        )
    if executable == "prettier":
        return "--check" in args
    if executable == "tsc":
        return "--noemit" in args
    python_action = compat._windows_python_action(tokens)
    if python_action is not None and python_action[0] == "module":
        return python_action[1].casefold() in {"compileall", "json.tool", "py_compile"}
    return False


def _windows_tokens_are_behavioral_validation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tokens = compat._windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable == "pytest":
        return not compat._pytest_args_request_no_test_execution(tokens[1:])
    if executable in {"ava", "cypress", "jest", "mocha", "playwright", "rspec", "tap", "tox", "vitest"}:
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        command_args = args[1:] if args[:1] == ["run"] else args
        return bool(command_args and (command_args[0] == "test" or command_args[0].startswith("test:")))
    if executable in {"node", "nodejs"}:
        return "--test" in args
    python_action = compat._windows_python_action(tokens)
    if python_action is not None and python_action[0] == "module":
        module = python_action[1].casefold()
        if module == "pytest":
            return not compat._pytest_args_request_no_test_execution(python_action[2])
        return module in {"nose", "nose2", "tox", "unittest"}
    if executable in {"cargo", "dotnet", "go", "gradle", "make", "mvn", "swift"}:
        return bool(args and args[0] == "test")
    return compat._windows_tokens_execute_script(tokens, require_test_name=True)


def _windows_tokens_execute_script(tokens: list[str], *, require_test_name: bool = False) -> bool:
    if not tokens:
        return False
    tokens = compat._windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    script: str | None = None
    python_action = compat._windows_python_action(tokens)
    if python_action is not None and python_action[0] == "script":
        script = python_action[1]
    elif executable in {"node", "nodejs", "ruby"} and len(tokens) > 1:
        non_options = [token for token in tokens[1:] if not token.startswith("-")]
        if non_options:
            script = non_options[0]
    elif executable.endswith((".js", ".mjs", ".cjs", ".py", ".ps1", ".rb")):
        script = executable
    if not script:
        return False
    normalized = script.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if not require_test_name:
        return True
    stem = normalized.rsplit(".", 1)[0]
    return bool(compat.re.search(r"(^|[._-])tests?([._-]|$)", stem))


def _windows_tokens_are_read_only_inspection(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if compat._windows_tokens_are_git_inspection(tokens):
        return True
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable in {"cat", "get-content", "head", "tail", "type", "wc"}:
        return bool(args)
    if executable in {"dir", "get-childitem", "ls", "pwd", "get-location"}:
        return not any(arg in {"-recurse", "/s"} for arg in args)
    if executable in {"grep", "rg", "select-string"}:
        return len(args) >= 2
    if executable == "find":
        return not any(arg in {"-delete", "-exec", "-execdir"} for arg in args)
    return False


def _windows_tokens_are_behavior_demo(tokens: list[str], *, payload: str, changed_paths: list[str]) -> bool:
    if not tokens:
        return False
    tokens = compat._windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    python_action = compat._windows_python_action(tokens)
    if python_action is not None and python_action[0] == "command":
        return True
    if executable in {"node", "nodejs", "ruby"} and any(flag in args for flag in {"-c", "-e"}):
        return True
    if compat._windows_tokens_execute_script(tokens):
        return True
    lowered = payload.casefold()
    if compat.re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)", lowered):
        return True
    normalized_payload = lowered.replace("\\", "/")
    return any(
        path.replace("\\", "/").lstrip("./").casefold() in normalized_payload
        for path in changed_paths
        if path and not compat._is_internal_runtime_path(path, project_root=None, task_path=None)
    )


def _canonical_restart_command(command: str) -> str:
    current = compat._normalize_command(command)
    for _ in range(6):
        wrapper = compat.windows_shell_wrapper_payload(current)
        if wrapper is not None:
            _shell_kind, payload, _problem = wrapper
            if payload:
                nested = compat._normalize_command(payload)
                if nested and nested != current:
                    current = nested
                    continue
            break
        try:
            parts = compat.shlex.split(current)
        except ValueError:
            break
        if len(parts) < 3 or compat.Path(parts[0]).name not in compat._RESTART_SHELL_NAMES:
            break
        command_index = next(
            (
                index + 1
                for index, token in enumerate(parts[1:-1], start=1)
                if token.startswith("-")
                and not token.startswith("--")
                and "c" in token[1:]
            ),
            None,
        )
        if command_index is None or command_index != len(parts) - 1:
            break
        nested = compat._normalize_command(parts[command_index])
        if not nested or nested == current:
            break
        current = nested
    return current


def _triggering_action_from_item(item: compat.Any, *, item_id: str | None, summary: str) -> compat.TriggeringAction:
    if not isinstance(item, dict):
        return compat.TriggeringAction(item_id=item_id, kind="item", status="completed", summary=summary)
    kind = str(item.get("type") or "item")
    exit_code = item.get("exitCode")
    return compat.TriggeringAction(
        item_id=item_id,
        kind=kind,
        command=item.get("command") if isinstance(item.get("command"), str) else None,
        cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else None,
        paths=compat._paths_from_item(item),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        status=item.get("status") if isinstance(item.get("status"), str) else "completed",
        timed_out=compat._item_explicitly_timed_out(item),
        summary=summary,
    )


def _item_explicitly_timed_out(item: dict[str, compat.Any]) -> bool:
    if item.get("timedOut") is True or item.get("timed_out") is True:
        return True
    explicit_statuses = {
        str(item.get(key) or "").strip().lower().replace("_", "")
        for key in ("status", "terminationReason", "termination_reason", "errorType", "error_type")
    }
    return bool({"timeout", "timedout"} & explicit_statuses)


def _validation_from_action(
    action: compat.TriggeringAction,
    *,
    sequence: int,
    item: compat.Any = None,
    changed_paths: list[str] | None = None,
) -> compat.ValidationRun | None:
    if action.kind != "commandExecution" or not action.command:
        return None
    output = compat._command_output_from_item(item)
    validation_type = compat._classify_validation_command(
        action.command, changed_paths=changed_paths or [], output=output,
    )
    if validation_type is None:
        return None
    normalized_command = compat._normalize_command(action.command)
    raw_selector = compat._raw_validation_selector(action.command)
    executed_test_names = compat._executed_test_names(action.command, output)
    executed_test_files = compat._test_files_from_output(output)
    outcome = "pass" if action.exit_code == 0 else "fail"
    if validation_type == "behavioral" and outcome == "pass" and not compat._tests_executed(action.command, output):
        outcome = "fail"
    trusted_outcome = "passed" if outcome == "pass" else "failed"
    passed = outcome == "pass"
    passed_count, failed_count = compat._test_count_summary(output)
    # Trust the test runner's factual result over the enclosing shell status. This preserves
    # real failures without reviving the removed shell-shape/masking classifier.
    if validation_type == "behavioral" and failed_count is not None and failed_count > 0:
        outcome = "fail"
        trusted_outcome = "failed"
        passed = False
    summary = compat._validation_summary(action.summary, output)
    return compat.ValidationRun(
        validation_id=compat._stable_validation_id(
            normalized_command=normalized_command,
            cwd=action.cwd,
            validation_type=validation_type,
            raw_selector=raw_selector,
            executed_test_names=executed_test_names,
        ),
        command=action.command,
        raw_command=action.command,
        normalized_command=normalized_command,
        cwd=action.cwd,
        exit_code=action.exit_code,
        shell_exit_code=action.exit_code,
        type=validation_type,
        outcome=outcome,
        passed=passed,
        trusted_validation_outcome=trusted_outcome,
        masking_reason=None,
        summary=summary,
        captured_output=output,
        captured_output_truncated=output.endswith("...<truncated>"),
        sequence=sequence,
        was_filtered=compat._command_was_filtered(action.command),
        raw_selector=raw_selector,
        executed_test_names=executed_test_names,
        executed_test_files=executed_test_files,
        passed_count=passed_count,
        failed_count=failed_count,
        target_files_or_test_files=compat._target_files_or_test_files(action.command),
    )


def _inspection_from_action(
    action: compat.TriggeringAction,
    *,
    sequence: int,
    item: compat.Any = None,
) -> compat.InspectionRun | None:
    if action.kind == "fileRead" and isinstance(item, dict) and item.get("tool") in {
        "read_file", "search", "list_directory", "view_image"
    }:
        # Native managed reads are evidence too. Keep their actual tool identity
        # instead of inventing a shell command that was never executed.
        operation = "tool:" + item["tool"] + " " + compat.json.dumps(item.get("arguments", {}), sort_keys=True, ensure_ascii=False)
        output = compat._command_output_from_item(item)
        passed = action.exit_code == 0 and action.status == "completed"
        return compat.InspectionRun(
            inspection_id=compat._stable_inspection_id(normalized_command=operation, cwd=action.cwd, inspected_paths=action.paths),
            command=operation, raw_command=operation, normalized_command=operation,
            cwd=action.cwd, exit_code=action.exit_code, shell_exit_code=None,
            outcome="pass" if passed else "fail", passed=passed,
            summary=compat._validation_summary(action.summary, output), captured_output=output,
            captured_output_truncated=output.endswith("...<truncated>"), sequence=sequence,
            inspected_paths=action.paths,
        )
    if action.kind != "commandExecution" or not action.command:
        return None
    if not compat._is_read_only_inspection_command(action.command):
        return None
    output = compat._command_output_from_item(item)
    normalized_command = compat._normalize_command(action.command)
    inspected_paths = compat._inspected_paths_from_command(action.command)
    outcome = "pass" if compat._inspection_exit_is_usable(action.command, action.exit_code) else "fail"
    summary = compat._validation_summary(action.summary, output)
    return compat.InspectionRun(
        inspection_id=compat._stable_inspection_id(
            normalized_command=normalized_command,
            cwd=action.cwd,
            inspected_paths=inspected_paths,
        ),
        command=action.command,
        raw_command=action.command,
        normalized_command=normalized_command,
        cwd=action.cwd,
        exit_code=action.exit_code,
        shell_exit_code=action.exit_code,
        outcome=outcome,
        passed=outcome == "pass",
        summary=summary,
        captured_output=output,
        captured_output_truncated=output.endswith("...<truncated>"),
        sequence=sequence,
        inspected_paths=inspected_paths,
    )


def _classify_validation_command(
    command: str, *, changed_paths: list[str], output: str = "",
) -> str | None:
    # A test followed by a syntax/diff check still supplies behavioral evidence.
    # Classify individual shell segments first, preserving static-only commands
    # such as `node --check game.test.js` and ignoring quoted/printed test names.
    inner = compat._shell_command_payload(command)
    segments = compat._posix_validation_command_segments(inner if inner is not None else command)
    # A shell branch can skip the named tests and still exit zero. Promote a
    # mixed command only with runner evidence, not merely a test name in argv.
    has_runner_output = compat._captured_output_looks_like_test_runner(output) or any(
        count is not None for count in compat._test_count_summary(output)
    )
    if segments is not None and len(segments) > 1 and has_runner_output:
        for segment in segments:
            segment_command = compat.shlex.join(segment)
            if compat._is_observationless_output_command(segment_command):
                continue
            if compat._classify_validation_command(segment_command, changed_paths=changed_paths) == "behavioral":
                return "behavioral"
    if compat._is_git_inspection_command(command):
        return "static" if compat._is_git_diff_check_command(command) else None
    if compat._is_read_only_inspection_command(command):
        return None
    if compat._is_static_validation_command(command):
        return "static"
    if compat._is_behavioral_validation_command(command):
        return "behavioral"
    if compat._is_behavior_demo_command(command, changed_paths=changed_paths):
        return "behavior_demo"
    return None


def _is_static_validation_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and compat._windows_tokens_are_static_validation(windows_tokens))
    inner = compat._shell_command_payload(command)
    if inner is not None and inner != command:
        return compat._is_static_validation_command(inner)
    lowered = command.lower()
    executable_prefix = r"(^|[\s;&|()'\"])(?:npx\s+|(?:\.{0,2}/|/)?(?:[\w.-]+/)*)"
    node_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*node(?:js)?"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    patterns = (
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+-c(\s|$)",
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+--check(\s|$)",
        r"(^|[\s;&|()'\"])git\s+diff\s+--check(\s|$)",
        executable_prefix + r"eslint(\s|$)",
        r"(^|[\s;&|()'\"])(npm|pnpm|yarn)\s+(run\s+)?lint(\s|$|:)",
        r"(^|[\s;&|()'\"])(npm|pnpm|yarn)\s+(run\s+)?type-?check(\s|$|:)",
        executable_prefix + r"prettier\s+--check(\s|$)",
        executable_prefix + r"tsc(?:\s+[^;&|()]*)?\s+--noemit(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + r"\s+-m\s+(py_compile|compileall)(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + r"\s+-m\s+json\.tool(\s|$)",
        r"(^|[\s;&|()'\"])jq\s+['\"]?\.['\"]?(\s|$)",
        r"json\.parse\s*\(",
    )
    return any(compat.re.search(pattern, lowered) for pattern in patterns)


def _is_git_inspection_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and compat._windows_tokens_are_git_inspection(windows_tokens))
    inner = compat._shell_command_payload(command)
    if inner is not None and inner != command:
        return compat._is_git_inspection_command(inner)
    lowered = command.lower()
    pattern = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*git\s+(diff|status|log|show|branch|remote|rev-parse|for-each-ref)\b"
    return bool(compat.re.search(pattern, lowered))


def _is_git_diff_check_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(
            windows_tokens
            and compat._windows_tokens_are_git_inspection(windows_tokens)
            and len(windows_tokens) > 1
            and windows_tokens[1].casefold() == "diff"
            and "--check" in (token.casefold() for token in windows_tokens[2:])
        )
    inner = compat._shell_command_payload(command)
    if inner is not None and inner != command:
        return compat._is_git_diff_check_command(inner)
    lowered = command.lower()
    pattern = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*git\s+diff(?:\s+[^;&|()'\"]+)*\s+--check(\s|$)"
    return bool(compat.re.search(pattern, lowered))


def _is_read_only_inspection_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and compat._windows_tokens_are_read_only_inspection(windows_tokens))
    inner = compat._shell_command_payload(command)
    if inner is not None and inner != command:
        return compat._is_read_only_inspection_command(inner)
    lowered = command.lower()
    if any(marker in lowered for marker in ("<<", "$(", "`")):
        return False
    if compat.re.search(r"(?<![12])>(?!&)", command) or compat.re.search(r"(^|[^<])<(?!<)", command):
        return False
    segments = compat._inspection_command_segments(command)
    if segments is None:
        return False
    if not segments:
        return False
    return all(compat._is_read_only_inspection_tokens(segment) for segment in segments)


def _inspection_command_segments(command: str) -> list[list[str]] | None:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return [windows_tokens] if windows_tokens else None
    try:
        lexer = compat.shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = [token for token in lexer if token]
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {"&&", ";", "|"}:
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        if token in {"&"} or any(char in token for char in "<>"):
            return None
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _shell_command_payload(command: str) -> str | None:
    try:
        tokens = compat.shlex.split(command)
    except ValueError:
        return None
    tokens = compat._strip_env_command_prefix(tokens)
    if len(tokens) < 3:
        return None
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable not in {"bash", "sh", "zsh"}:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if not token.startswith("-"):
            continue
        if "c" not in token[1:]:
            continue
        if index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _strip_env_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and compat.re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
        remaining = remaining[1:]
    if not remaining:
        return remaining
    executable = remaining[0].rsplit("/", 1)[-1].lower()
    if executable != "env":
        return remaining
    remaining = remaining[1:]
    while remaining:
        token = remaining[0]
        if token == "--":
            remaining = remaining[1:]
            break
        if compat.re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", token):
            remaining = remaining[1:]
            continue
        if token.startswith("-"):
            remaining = remaining[1:]
            continue
        break
    return remaining


def _is_read_only_inspection_segment(segment: str) -> bool:
    try:
        tokens = compat.shlex.split(segment)
    except ValueError:
        return False
    return compat._is_read_only_inspection_tokens([token for token in tokens if token])


def _is_read_only_inspection_tokens(tokens: list[str]) -> bool:
    while tokens and compat.re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = [token.lower() for token in tokens[1:]]
    if executable == "git":
        return bool(args) and args[0] in {"diff", "status", "log", "show", "branch", "remote", "rev-parse", "for-each-ref"}
    if executable in {"cat", "sed", "grep", "egrep", "fgrep", "rg", "head", "tail", "nl", "ls", "wc", "pwd", "stat", "file", "find"}:
        if executable == "find" and any(arg in {"-delete", "-exec", "-execdir"} for arg in args):
            return False
        return True
    return False


def _is_behavioral_validation_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and compat._windows_tokens_are_behavioral_validation(windows_tokens))
    return any(compat._posix_tokens_are_behavioral_validation(tokens)
               for tokens in compat._posix_validation_command_segments(command) or [])


def _posix_validation_command_segments(command: str) -> list[list[str]] | None:
    """Find literal command positions, never test names in arbitrary argv.

    Reuse the quote-aware splitter: a printed/escaped ';' is not a new command.
    This is evidence classification, not a shell evaluator or execution gate.
    Unsupported syntax is not promoted to trusted test evidence.
    """
    parts = compat._literal_parts(command)
    if parts is None:
        return None
    segments: list[list[str]] = []
    for index, (text, operator) in enumerate(parts):
        try:
            tokens = compat._strip_env_command_prefix(compat.shlex.split(text))
        except ValueError:
            return None
        if not tokens:
            if index == len(parts) - 1 and operator is None:
                continue
            return None
        inner = compat._shell_command_payload(compat.shlex.join(tokens))
        if inner is not None:
            nested = compat._posix_validation_command_segments(inner)
            if nested is None:
                return None
            segments.extend(nested)
        else:
            segments.append(tokens)
    return segments


def _posix_tokens_are_behavioral_validation(tokens: list[str]) -> bool:
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]
    if executable == "npx":
        while args and args[0] in {"--yes", "-y", "--no-install"}:
            args = args[1:]
        return bool(args and not args[0].startswith("-")
                    and compat._posix_tokens_are_behavioral_validation(args))
    if executable == "pytest":
        return not compat._pytest_args_request_no_test_execution(args)
    if executable in {"mocha", "jest", "ava", "tap", "vitest", "playwright", "cypress", "tox", "rspec"}:
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        args = args[1:] if args[:1] == ["run"] else args
        return bool(args and (args[0] == "test" or args[0].startswith("test:")))
    if executable in {"node", "nodejs"} and args[:1] == ["--test"]:
        return True
    # The existing Python action parser consumes option values and stops at
    # the actual script/-c/-m operand, rather than scanning later arguments.
    python_action = compat._windows_python_action([executable, *args])
    if python_action is not None and python_action[0] == "module":
        module = python_action[1]
        if module == "pytest":
            return not compat._pytest_args_request_no_test_execution(python_action[2])
        return module in {"unittest", "tox", "nose", "nose2"}
    if executable in {"go", "cargo", "mvn", "gradle", "swift", "dotnet", "make"}:
        return args[:1] == ["test"]
    script = compat._posix_script_execution_operand(tokens)
    return bool(script and compat.re.search(r"(^|[._-])tests?([._-]|$)",
                                    script.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()))


def _posix_script_execution_operand(tokens: list[str]) -> str | None:
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    script = tokens[0]
    args = tokens[1:]
    python_action = compat._windows_python_action([executable, *args])
    if python_action is not None:
        script = python_action[1] if python_action[0] == "script" else ""
    elif compat.re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        return None
    elif executable in {"node", "nodejs", "ruby", "bash", "sh", "zsh"}:
        # Only known no-value execution options may precede a script. In
        # particular -c/-e inline code, --check/-n and flag operands aren't it.
        no_value_options = ({"--no-warnings", "--enable-source-maps"} if executable in {"node", "nodejs"}
                            else {"-w"} if executable == "ruby" else {"-e", "-u", "-x", "-eu", "-eux"})
        while args and args[0] in no_value_options:
            args = args[1:]
        if args[:1] == ["--"]:
            args = args[1:]
        if not args or args[0].startswith("-"):
            return None
        script = args[0]
    return script if compat.re.fullmatch(r"[\w./ -]+\.(?:py|js|mjs|cjs|rb|sh)", script, compat.re.IGNORECASE) else None


def _is_test_wrapper_script_command(command: str) -> bool:
    for tokens in compat._posix_validation_command_segments(command) or []:
        script = compat._posix_script_execution_operand(tokens)
        if script and compat.re.search(r"(^|[._-])tests?([._-]|$)", script.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()):
            return True
    return False


def _is_direct_script_execution_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and compat._windows_tokens_execute_script(windows_tokens))
    return any(compat._posix_script_execution_operand(tokens) is not None
               for tokens in compat._posix_validation_command_segments(command) or [])


def _is_behavior_demo_command(command: str, *, changed_paths: list[str]) -> bool:
    windows_surface, windows_tokens, payload = compat._windows_classification_tokens(command)
    if windows_surface:
        return bool(
            windows_tokens
            and payload
            and compat._windows_tokens_are_behavior_demo(
                windows_tokens,
                payload=payload,
                changed_paths=changed_paths,
            )
        )
    lowered = command.lower()
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    node_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*node(?:js)?"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    ruby_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*ruby"
    inline_patterns = (
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+-e(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+-c(\s|$)",
        r"(^|[\s;&|()'\"])" + ruby_exec + r"\s+-e(\s|$)",
    )
    http_patterns = (
        r"(^|[\s;&|()'\"])(curl|wget|http|https)\s+",
        r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)",
    )
    return (
        (compat._has_behavior_demo_marker(command) and compat._marked_behavior_demo_command_is_plausible(command, changed_paths))
        or compat._is_direct_script_execution_command(command)
        or compat._is_stdin_script_demo_command(command)
        or compat._command_requires_changed_module(command, changed_paths)
        or any(compat.re.search(pattern, lowered) for pattern in inline_patterns)
        or any(compat.re.search(pattern, lowered) for pattern in http_patterns)
    )


def _has_behavior_demo_marker(command: str) -> bool:
    return bool(compat.re.search(r"\bBELLO_BEHAVIOR_DEMO\s*=\s*(?:1|true|yes)\b", command, compat.re.IGNORECASE))


def _marked_behavior_demo_command_is_plausible(command: str, changed_paths: list[str]) -> bool:
    if compat._is_read_only_inspection_command(command):
        return False
    if compat._is_observationless_output_command(command):
        return False
    if compat._is_direct_script_execution_command(command) or compat._is_stdin_script_demo_command(command):
        return True
    if compat._command_requires_changed_module(command, changed_paths):
        return True
    lowered = command.lower()
    if compat.re.search(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)", lowered):
        return True
    normalized_command = lowered.replace("\\", "/")
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/").lstrip("./").lower()
        if not path or compat._is_internal_runtime_path(path, project_root=None, task_path=None):
            continue
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if path in normalized_command or (stem and len(stem) >= 3 and stem in normalized_command):
            return True
    return bool(compat.re.search(r"(^|[\s;&|()'\"])(?:\.{1,2}/|/)[\w./-]+(?:\s|$)", lowered))


def _is_observationless_output_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = compat._windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens:
            return False
        return windows_tokens[0] in {
            "cat",
            "echo",
            "false",
            "get-content",
            "head",
            "ls",
            "printf",
            "pwd",
            "rg",
            "tail",
            "true",
            "type",
            "wc",
            "yes",
        }
    segments = [segment.strip() for segment in compat.re.split(r"\s*(?:&&|;|\|)\s*", command) if segment.strip()]
    if not segments:
        return False
    output_only = {"echo", "printf", "true", "false", "yes"}
    read_only_excerpt = {"cat", "sed", "grep", "egrep", "fgrep", "rg", "head", "tail", "nl", "ls", "wc", "pwd"}
    seen_executable = False
    for segment in segments:
        try:
            tokens = compat.shlex.split(segment)
        except ValueError:
            return False
        while tokens and compat.re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1].lower()
        seen_executable = True
        if executable not in output_only and executable not in read_only_excerpt:
            return False
    return seen_executable


def _is_stdin_script_demo_command(command: str) -> bool:
    lowered = command.lower()
    if "<<" not in lowered:
        return False
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:node(?:js)?|ruby|bash|sh|zsh)"
    patterns = (
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+-?\s*<<",
        r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+-?\s*<<",
    )
    return any(compat.re.search(pattern, lowered) for pattern in patterns)


def _command_requires_changed_module(command: str, changed_paths: list[str]) -> bool:
    lowered = command.lower()
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:node(?:js)?|python(?:3(?:\.\d+)?)?|ruby)"
    if not compat.re.search(r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+(-e|-c|\S+)", lowered):
        return False
    if not compat.re.search(r"\b(require|import|node|nodejs|python|python3|ruby)\b", lowered):
        return False
    normalized_command = lowered.replace("\\", "/")
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/").lstrip("./").lower()
        if not path or compat._is_internal_runtime_path(path, project_root=None, task_path=None):
            continue
        candidates = {path}
        if path.endswith((".js", ".ts", ".jsx", ".tsx", ".py", ".rb")):
            candidates.add(path.rsplit(".", 1)[0])
        if any(candidate and candidate in normalized_command for candidate in candidates):
            return True
    return False


def _tests_executed(command: str, output: str) -> bool:
    if not compat._is_behavioral_validation_command(command):
        return True
    lowered = output.lower()
    zero_test_patterns = (
        # Do not treat npm's `package@1.0.0 test` header as zero tests.
        r"(?<![\w.])0[ \t]+(passing|failing|pending|tests?|specs?)\b",
        r"(?<![\w.])0[ \t]+tests?[ \t]+(run|executed|passed|failed|total)\b",
        r"(?m)^[ \t]*[#ℹ][ \t]+tests[ \t]+0[ \t]*$",
        r"\btests?:\s+0\s+total\b",
        r"\btest suites?:\s+0\b",
        r"\bran\s+0\s+tests?\b",
        r"\bno tests?\s+(found|run|executed)\b",
    )
    return not any(compat.re.search(pattern, lowered) for pattern in zero_test_patterns)


def _command_output_from_item(item: compat.Any, *, limit: int = 20000) -> str:
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    compat._collect_output_strings(item, parts, depth=0)
    return compat._bounded_text("\n".join(parts), limit=limit)


def _item_with_recorded_output(item: compat.Any, output: str) -> compat.Any:
    if not output or not isinstance(item, dict):
        return item
    existing = compat._command_output_from_item(item)
    if existing.strip() == output.strip():
        merged = existing
    else:
        merged = output if not existing else f"{existing}\n{output}"
    enriched = dict(item)
    enriched["output"] = merged
    return enriched


def _output_delta_text(params: dict[str, compat.Any], *, limit: int = 20000) -> str:
    parts: list[str] = []
    compat._collect_output_delta_strings(params, parts, depth=0)
    return compat._bounded_text("".join(parts), limit=limit)


def _collect_output_delta_strings(value: compat.Any, parts: list[str], *, depth: int) -> None:
    if depth > 5:
        return
    if isinstance(value, str):
        if value:
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            compat._collect_output_delta_strings(item, parts, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        key_text = str(key).lower()
        if key_text in {
            "delta",
            "output",
            "outputtext",
            "aggregatedoutput",
            "aggregated_output",
            "combinedoutput",
            "combined_output",
            "stdout",
            "stdouttext",
            "stdout_text",
            "stderr",
            "stderrtext",
            "stderr_text",
            "text",
            "content",
            "message",
            "chunk",
            "data",
        }:
            compat._collect_output_delta_strings(nested, parts, depth=depth + 1)
        elif key_text in {"outputs", "chunks", "lines", "items"}:
            compat._collect_output_delta_strings(nested, parts, depth=depth + 1)


def _validation_summary(summary: str, output: str, *, limit: int = 4000) -> str:
    stripped = output.strip()
    if not stripped:
        return summary
    if stripped in summary:
        return summary
    return compat._bounded_text(f"{summary}\nOutput:\n{stripped}", limit=limit)


def _validation_id(sequence: int) -> str:
    return f"validation-{sequence}"


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def _stable_validation_id(
    *,
    normalized_command: str,
    cwd: str | None,
    validation_type: str,
    raw_selector: str | None,
    executed_test_names: list[str],
) -> str:
    payload = {
        "normalized_command": normalized_command,
        "cwd": cwd or "",
        "validation_type": validation_type,
        "raw_selector": raw_selector or "",
        "executed_test_names": sorted(dict.fromkeys(executed_test_names)),
    }
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"validation-{digest[:16]}"


def _stable_inspection_id(
    *,
    normalized_command: str,
    cwd: str | None,
    inspected_paths: list[str],
) -> str:
    payload = {
        "normalized_command": normalized_command,
        "cwd": cwd or "",
        "inspected_paths": sorted(dict.fromkeys(inspected_paths)),
    }
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"inspection-{digest[:16]}"


def _inspection_exit_is_usable(command: str, exit_code: int | None) -> bool:
    if exit_code == 0:
        return True
    if exit_code == 1 and compat.re.search(r"(^|[\s;&|()'\"])(?:rg|grep|egrep|fgrep)\b", command.lower()):
        return True
    return False


def _command_was_filtered(command: str) -> bool:
    return compat._raw_validation_selector(command) is not None


def _raw_validation_selector(command: str) -> str | None:
    windows_surface, windows_tokens, payload = compat._windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return None
        command = payload.replace("\\", "/")
    selectors: list[str] = []
    patterns = (
        r"(?:^|\s)(-k)\s+([^\s;&|]+)",
        r"(?:^|\s)(-m)\s+([^\s;&|]+)",
        r"(?:^|\s)(--grep|--testNamePattern|--test-name-pattern|--filter|--test)\s+([^\s;&|]+)",
        r"(?:^|\s)(-g)\s+([^\s;&|]+)",
    )
    for pattern in patterns:
        for match in compat.re.finditer(pattern, command):
            if match.group(1) == "-m" and compat._is_python_module_flag(command, match.start(1)):
                continue
            selector = match.group(2).strip("\"'")
            selectors.append(f"{match.group(1)} {selector}")
    for target in compat._explicit_test_selectors(command):
        selectors.append(target)
    return "; ".join(dict.fromkeys(selectors)) or None


def _is_python_module_flag(command: str, start: int) -> bool:
    prefix = command[:start].rstrip().lower()
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    pattern = r"(^|[\s;&|()'\"])(python|python3)" + python_flags + r"*$"
    return bool(compat.re.search(pattern, prefix))


def _explicit_test_selectors(command: str, *, limit: int = 50) -> list[str]:
    selectors: list[str] = []
    for match in compat.re.finditer(
        r"(?<![\w./-])(?:\.?/)?[\w./-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php)(?:::[\w.*\[\]-]+)+",
        command,
    ):
        selectors.append(match.group(0).strip("'\"").lstrip("./"))
        if len(selectors) >= limit:
            break
    return list(dict.fromkeys(selectors))


def _executed_test_names(command: str, output: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    names.extend(compat._explicit_test_selectors(command, limit=limit))
    names.extend(compat._test_names_from_output(output, limit=limit))
    if not names and compat._is_behavioral_validation_command(command):
        names.extend(compat._target_files_or_test_files(command))
    return list(dict.fromkeys(names))[:limit]


def _test_names_from_output(output: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    patterns = (
        r"(?m)\b([\w./+\[\]-]+::test_[\w.\[\]-]+)\b",
        r"(?m)\b(test_[A-Za-z0-9_]+)\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS)\b",
        r"(?m)\b(?:✓|PASS|FAIL)\s+([^()\n]{3,160})",
    )
    for pattern in patterns:
        for match in compat.re.finditer(pattern, output):
            name = " ".join(match.group(1).strip().split())
            if name:
                names.append(name)
            if len(names) >= limit:
                return list(dict.fromkeys(names))
    return list(dict.fromkeys(names))


def _test_files_from_output(output: str, *, limit: int = 100) -> list[str]:
    files: list[str] = []
    runner_patterns = (
        # Jest/Vitest style suite lines. Prefer these over the broad fallback so stack traces
        # through test helpers do not look like independently executed test files.
        r"(?m)^\s*(?:PASS|FAIL)\s+((?:\.{0,2}/)?[\w@+./-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php|vue|svelte|snap|snapshot|golden))\b",
        # Pytest verbose output.
        r"(?m)^\s*((?:\.{0,2}/)?[\w@+./-]+\.py)::[^\s]+\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b",
    )
    for pattern in runner_patterns:
        for match in compat.re.finditer(pattern, output):
            path = compat._normalize_output_test_path(match.group(1))
            if path and compat._file_kind(path) == "test":
                files.append(path)
            if len(files) >= limit:
                return list(dict.fromkeys(files))[:limit]
    if files:
        return list(dict.fromkeys(files))[:limit]

    path_pattern = compat.re.compile(
        r"(?<![\w./-])((?:\.{0,2}/)?[\w@+./-]*(?:test|spec|tests|__tests__|snapshots|__snapshots__|golden|goldens)"
        r"[\w@+./-]*\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php|snap|snapshot|golden))"
        r"(?:::[\w.*\[\]-]+)?",
        compat.re.IGNORECASE,
    )
    for match in path_pattern.finditer(output):
        path = compat._normalize_output_test_path(match.group(1))
        if path and compat._file_kind(path) == "test":
            files.append(path)
        if len(files) >= limit:
            break
    return list(dict.fromkeys(files))[:limit]


def _normalize_output_test_path(path: str) -> str:
    normalized = path.strip().strip("'\"`.,;:()[]{}<>")
    if "::" in normalized:
        normalized = normalized.split("::", 1)[0]
    return normalized.replace("\\", "/").lstrip("./")


def _test_count_summary(output: str) -> tuple[int | None, int | None]:
    lowered = output.lower()
    passed = compat._first_int_match(
        lowered,
        (
            r"\b(\d+)\s+passed\b",
            r"\b(\d+)\s+passing\b",
            r"\bpasses:\s*(\d+)\b",
            r"\btests?:\s*(\d+)\s+passed\b",
            r"(?m)^[ \t]*[#ℹ][ \t]+pass[ \t]+(\d+)[ \t]*$",
        ),
    )
    failed = compat._first_int_match(
        lowered,
        (
            r"\b(\d+)\s+failed\b",
            r"\b(\d+)\s+failing\b",
            r"\bfailures?:\s*(\d+)\b",
            r"\btests?:\s*\d+\s+passed,\s*(\d+)\s+failed\b",
        ),
    )
    # A zero-failure Node summary must not hide a failure from another runner
    # or an earlier Node invocation in the same shell command.
    node_failures = [
        int(match.group(1))
        for match in compat.re.finditer(r"(?m)^[ \t]*[#ℹ][ \t]+fail[ \t]+(\d+)[ \t]*$", lowered)
    ]
    if node_failures:
        failed = max(failed or 0, *node_failures)
    if passed is not None and failed is None:
        failed = 0
    return passed, failed


def _first_int_match(text: str, patterns: tuple[str, ...]) -> int | None:
    for pattern in patterns:
        match = compat.re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _collect_output_strings(value: compat.Any, parts: list[str], *, depth: int) -> None:
    if depth > 4:
        return
    if isinstance(value, str):
        if value.strip():
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            compat._collect_output_strings(item, parts, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        key_text = str(key).lower()
        if key_text in {
            "output",
            "outputtext",
            "aggregatedoutput",
            "aggregated_output",
            "combinedoutput",
            "combined_output",
            "stdout",
            "stdouttext",
            "stdout_text",
            "stderr",
            "stderrtext",
            "stderr_text",
            "text",
            "content",
            "message",
            "summary",
        }:
            compat._collect_output_strings(nested, parts, depth=depth + 1)
        elif key_text in {"outputs", "chunks", "lines", "items", "result", "results"}:
            compat._collect_output_strings(nested, parts, depth=depth + 1)


def _has_passing_behavioral_validation(validations: list[compat.ValidationRun]) -> bool:
    return any(compat._validation_is_usable_behavioral_pass(validation) for validation in validations)


def _is_behavior_proving_validation(validation: compat.ValidationRun) -> bool:
    return validation.type in {"behavioral", "behavior_demo"}


def _validation_is_usable_behavioral_pass(validation: compat.ValidationRun) -> bool:
    if (
        not compat._is_behavior_proving_validation(validation)
        or validation.outcome != "pass"
        or not validation.passed
        or validation.trusted_validation_outcome != "passed"
    ):
        return False
    if validation.type != "behavior_demo":
        return True
    return compat._validation_output_kind(
        validation,
        captured_output_present=bool(validation.captured_output.strip()),
    ) == "factual_observation_candidate"


def _action_timed_out(action: compat.TriggeringAction) -> bool:
    return action.timed_out


def _target_files_or_test_files(command: str) -> list[str]:
    windows_surface, windows_tokens, payload = compat._windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return []
        command = payload.replace("\\", "/")
    targets: list[str] = []
    for match in compat.re.finditer(
        r"(?<![\w./-])(?:\.?/)?[\w./-]+\.(?:py|ps1|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php)(?![\w.-])",
        command,
    ):
        target = match.group(0).strip("'\"")
        if target:
            targets.append(target.lstrip("./"))
    return list(dict.fromkeys(targets))


def _inspected_paths_from_command(command: str, *, limit: int = 50) -> list[str]:
    windows_surface, windows_tokens, payload = compat._windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return []
        tokens = windows_tokens
    else:
        tokens = []
    inner = compat._shell_command_payload(command)
    if not windows_surface and inner is not None and inner != command:
        return compat._inspected_paths_from_command(inner, limit=limit)
    targets: list[str] = []
    if not windows_surface:
        try:
            tokens = compat.shlex.split(command)
        except ValueError:
            tokens = command.split()
    option_value_flags = {"-f", "--file", "--config", "-C"}
    skip_next = False
    commands = {
        "cat",
        "sed",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "nl",
        "ls",
        "wc",
        "pwd",
        "stat",
        "file",
        "find",
        "get-childitem",
        "get-content",
        "get-location",
        "select-string",
        "type",
        "git",
        "diff",
        "status",
        "log",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "for-each-ref",
    }
    common_target_dirs = {"src", "lib", "app", "tests", "test", "include", "public", "packages", "pkg"}
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in option_value_flags:
            skip_next = True
            continue
        path_token = token.replace("\\", "/") if windows_surface else token
        stripped = path_token.strip("'\"").lstrip("./")
        if not stripped or stripped.startswith(("-", "/")) or stripped.casefold() in commands:
            continue
        if stripped == ".":
            targets.append(".")
        elif stripped in common_target_dirs:
            targets.append(stripped)
        elif "/" in stripped or compat.re.search(r"\.[A-Za-z0-9_-]{1,12}$", stripped):
            targets.append(stripped)
        if len(targets) >= limit:
            break
    return list(dict.fromkeys(targets))


def _paths_from_item(item: dict[str, compat.Any]) -> list[str]:
    paths: list[str] = []
    raw_paths = item.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path) for path in raw_paths if isinstance(path, str))
    file_changes = item.get("fileChanges")
    if isinstance(file_changes, dict):
        paths.extend(str(path) for path in file_changes)
    changes = item.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            for key in ("path", "filePath", "file_path", "filepath"):
                value = change.get(key)
                if isinstance(value, str):
                    paths.append(value)
    return list(dict.fromkeys(paths))
