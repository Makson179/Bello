"""Narrow exclusions from optional semantic compression, not an importance model.

Recognise explicit reads of requirements/instructions and actual CLI help
requests. Keep the whole normal, already-budgeted reply for mixed commands.
This bounded static recogniser neither executes code nor opens files. Dynamic
paths, arbitrary shell syntax and indirect Python data flow are not interpreted.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Any, Callable


_DOCUMENT_NAMES = frozenset({
    "task", "readme", "agents", "claude", "instructions", "spec", "specification",
})
_DOCUMENT_SUFFIXES = frozenset({
    "", ".md", ".markdown", ".mdx", ".txt", ".text", ".rst", ".adoc", ".json", ".yaml", ".yml",
})
_READERS = frozenset({"cat", "head", "tail", "nl", "less", "more", "get-content"})
_DATA_COMMANDS = frozenset({
    "echo", "printf", "test", "[", "true", "false", "touch", "cp", "mv", "rm", "tee",
})
# In these commonplace tools -h means human-readable, hostname, headers, etc.
_NOT_SHORT_HELP = frozenset({
    "du", "df", "ls", "sort", "free", "tar", "ps", "ssh", "scp", "chown", "chgrp", "ln", "readlink", "grep",
})
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?$", re.I)
_HEREDOC = re.compile(
    r"(?m)^(?P<header>[^\n]*?)<<-?\s*['\"]?(?P<marker>[A-Za-z_][A-Za-z_0-9]*)['\"]?[^\n]*\n"
    r"(?P<body>.*?)(?m:^[\t ]*(?P=marker)[\t ]*$)", re.S,
)
_MAX_COMMAND_CHARS = 262_144


def _path_key(value: str | Path, workspace: Path | None) -> str:
    path = os.fspath(value)
    if not os.path.isabs(path) and workspace is not None:
        path = os.path.join(os.fspath(workspace), path)
    return os.path.normpath(path)


def _document_path(value: str, task_path: Path | None, workspace: Path | None) -> bool:
    if not value or "\n" in value or "\x00" in value:
        return False
    if task_path is not None and _path_key(value, workspace) == _path_key(task_path, workspace):
        return True
    path = PurePosixPath(value.replace("\\", "/").casefold())
    return path.stem in _DOCUMENT_NAMES and path.suffix in _DOCUMENT_SUFFIXES


def _python_read(source: str, important: Callable[[str], bool], depth: int) -> bool:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "open" and node.args:
            path = node.args[0]
            mode = node.args[1] if len(node.args) > 1 else next(
                (kw.value for kw in node.keywords if kw.arg == "mode"), ast.Constant("r"))
            if (isinstance(path, ast.Constant) and isinstance(path.value, str)
                    and isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                    and not any(flag in mode.value for flag in "wax+") and important(path.value)):
                return True
        if not isinstance(fn, ast.Attribute):
            continue
        if fn.attr in {"read_text", "read_bytes"} and isinstance(fn.value, ast.Call):
            constructor = fn.value
            name = constructor.func
            is_path = (isinstance(name, ast.Name) and name.id == "Path") or (
                isinstance(name, ast.Attribute) and isinstance(name.value, ast.Name)
                and name.value.id == "pathlib" and name.attr == "Path")
            if is_path and constructor.args and all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in constructor.args
            ) and important(os.path.join(*(arg.value for arg in constructor.args))):
                return True
        if (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"
                and fn.attr in {"run", "check_output", "check_call", "call", "Popen"}):
            value = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "args"), None)
            if isinstance(value, (ast.List, ast.Tuple)) and all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in value.elts
            ) and _argv_read([arg.value for arg in value.elts], important, depth + 1):
                return True
            if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and any(kw.arg == "shell" and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True for kw in node.keywords)
                    and _shell_read(value.value, important, depth + 1)):
                return True
    return False


def _argv_read(argv: list[str], important: Callable[[str], bool], depth: int, stdin: str = "") -> bool:
    if not argv or depth > 4:
        return False
    argv = list(argv)
    while argv and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", argv[0], re.S):
        argv.pop(0)
    if not argv:
        return False
    program = PurePosixPath(argv[0]).name.casefold()
    args = argv[1:]
    if program in {"for", "do", "done", "if", "then", "else", "fi", "while", "until", "case", "esac"}:
        return False
    if program in {"env", "command", "exec", "timeout", "sudo"}:
        # Only the common literal forms; unknown wrapper options do not guess.
        while args and (args[0].startswith("-") or "=" in args[0]):
            option = args.pop(0)
            if option in {"-u", "--unset", "-C", "--chdir", "-g", "--user", "--group"} and args:
                args.pop(0)
        if program == "timeout" and args:
            args.pop(0)
        return _argv_read(args, important, depth + 1, stdin)
    if program in {"sh", "bash", "zsh", "pwsh", "powershell"}:
        for index, arg in enumerate(args[:-1]):
            if arg in {"-c", "-lc", "-command", "-Command"}:
                return _shell_read(args[index + 1], important, depth + 1)
        return False
    if _PYTHON.fullmatch(program):
        if "-c" in args:
            index = args.index("-c")
            return index + 1 < len(args) and _python_read(args[index + 1], important, depth)
        if stdin and "-" in args:
            return _python_read(stdin, important, depth)
    if program in _DATA_COMMANDS:
        return False
    options = args[:args.index("--")] if "--" in args else args
    if "--help" in options or ("-h" in options and program not in _NOT_SHORT_HELP):
        return True
    if program in {"help", "man", "get-help"}:
        return True
    if program in _READERS:
        # Output redirection names a destination, not a document being read.
        paths: list[str] = []
        skip_destination = False
        for arg in args:
            if skip_destination:
                skip_destination = False
            elif re.fullmatch(r"\d*>>?", arg):
                skip_destination = True
            elif not arg.startswith("-") and not re.match(r"\d*>", arg):
                paths.append(arg)
        return any(important(arg) for arg in paths)
    if program in {"sed", "grep", "rg"}:
        if program == "sed" and any(arg.startswith(("-i", "--in-place")) for arg in options):
            return False
        # Skip the literal expression; a document name in a pattern is not a read.
        files: list[str] = []
        expression_seen = False
        consume_expression = False
        for arg in args:
            if consume_expression:
                expression_seen, consume_expression = True, False
            elif arg in {"-e", "--expression", "--regexp"}:
                consume_expression = True
            elif arg.startswith("-"):
                continue
            elif not expression_seen:
                expression_seen = True
            else:
                files.append(arg)
        return any(important(arg) for arg in files)
    return bool(args and args[0] == "help")


def _split_commands(source: str) -> list[list[str]]:
    lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    argv: list[str] = []
    for token in lexer:
        if token and all(char in ";&|()\n" for char in token):
            if argv:
                commands.append(argv)
            argv = []
        else:
            argv.append(token)
    if argv:
        commands.append(argv)
    return commands


def _commands_read(commands: list[list[str]], important: Callable[[str], bool], depth: int,
                   stdin: str = "") -> bool:
    for index, argv in enumerate(commands):
        # Recognise only a flat `for name in <literal values>; do ...; done`.
        # Values alone are not help requests: inspect their actual body use.
        if (len(argv) >= 4 and argv[0] == "for" and argv[2] == "in"
                and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", argv[1])
                and len(argv[3:]) <= 32
                and not any(re.search(r"[$`*?]", value) for value in argv[3:])):
            end = next((pos for pos in range(index + 1, len(commands))
                        if commands[pos][0] in {"for", "done"}), None)
            body = commands[index + 1:end] if end is not None else []
            if body and body[0][0] == "do" and commands[end][0] == "done":
                body = [body[0][1:], *body[1:]]
                references = {"$" + argv[1], "${" + argv[1] + "}"}
                for value in argv[3:]:
                    for call in body:
                        expanded = [value if arg in references else arg for arg in call]
                        if _argv_read(expanded, important, depth, stdin):
                            return True
        if _argv_read(argv, important, depth, stdin):
            return True
    return False


def _shell_read(source: str, important: Callable[[str], bool], depth: int = 0) -> bool:
    if depth > 4 or len(source) > _MAX_COMMAND_CHARS:
        return False
    found = False

    def heredoc(match: re.Match[str]) -> str:
        nonlocal found
        try:
            commands = _split_commands(match["header"])
        except ValueError:
            return ""
        found = found or _commands_read(commands, important, depth, match["body"])
        return "\n"

    source = _HEREDOC.sub(heredoc, source)
    if found:
        return True
    try:
        return _commands_read(_split_commands(source), important, depth)
    except ValueError:
        return False


def preserve_tool_output(
    name: str, args: dict[str, Any], *, command: str | None = None,
    task_path: Path | None = None, workspace: Path | None = None,
) -> bool:
    """Preserve selected critical replies without changing capture/output budgets.

    ``command`` is the original exec request for a subsequent poll/stop. The
    configured task is compared by exact path, never just its arbitrary basename.
    """
    if task_path is not None:
        task_path = Path(_path_key(task_path, workspace))
    if name in {"exec_command", "poll_command", "stop_command"}:
        source = command if command is not None else args.get("command")
        cwd = args.get("cwd")
        command_root = Path(_path_key(cwd, workspace)) if isinstance(cwd, str) else workspace
        important = lambda value: _document_path(value, task_path, command_root)
        return isinstance(source, str) and _shell_read(source, important)
    if name not in {"read_file", "search"}:
        return False
    path = args.get("path")
    return isinstance(path, str) and _document_path(path, task_path, workspace)
