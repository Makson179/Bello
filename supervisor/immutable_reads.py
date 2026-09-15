"""Recognize only literal pinned-file reads for the immutable-path hard deny.

This is not an execution allowlist or a shell evaluator. Its result is used
only by the immutable-path check; the unchanged command still goes through
the normal policy and, for mixed/ambiguous commands, semantic review.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil


_READERS = {"cat", "head", "tail", "wc", "shasum", "sha1sum", "sha224sum",
            "sha256sum", "sha384sum", "sha512sum", "md5sum", "cksum"}
_SHELLS = {"sh", "bash", "zsh"}
_SHELL_STATE_COMMANDS = {"alias", "unalias", "function", "source", ".", "eval", "exec",
    "export", "unset", "set", "typeset", "readonly", "hash", "rehash", "cd", "pushd", "popd",
    "command", "builtin", "time", "!", "if", "then", "else", "elif", "fi", "for", "while",
    "until", "case", "esac", "do", "done", "select", "repeat", "coproc", "autoload", "enable",
    "disable", "emulate", "setopt", "unsetopt", "read", "getopts"}


def _system_command(raw: str, names: set[str], *, workspace: Path | None = None) -> bool:
    path = Path(raw)
    if path.name not in names or (not path.is_absolute() and path.parent != Path(".")):
        return False
    try:
        if not path.is_absolute():
            # which() runs in Bello's host cwd, execution runs in the assigned
            # workspace. Relative/empty PATH entries could resolve differently;
            # a workspace-controlled absolute PATH entry could change later.
            for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
                entry = Path(raw_entry)
                if not raw_entry or not entry.is_absolute():
                    return False
                if workspace is not None and (
                    entry.is_relative_to(workspace) or entry.resolve().is_relative_to(workspace.resolve())
                ):
                    return False
        executable = shutil.which(raw)
        if executable is None:
            return False
        lexical = Path(executable).absolute()
        canonical = lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    # Neither a workspace executable nor an arbitrary host PATH shim gets a
    # claim to the semantics of cat/shasum. Keep this POSIX-only and literal.
    system_bins = {Path("/bin"), Path("/usr/bin")}
    return lexical.parent in system_bins and canonical.parent in system_bins


def _literal_parts(command: str) -> list[tuple[str, str | None]] | None:
    """Split only unquoted separators; retain quoting inside each command.

    ``shlex`` alone drops quotes around ';' and '|', which must never let a
    string argument to an unknown program masquerade as a verified reader.
    No substitutions, grouping, continuations or quoted newlines are accepted.
    """
    parts = []
    quote = None
    escaped = False
    start = index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            if char in "\r\n":
                return None
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif char in "\"'" and (quote is None or quote == char):
            quote = char if quote is None else None
        elif char in "\r\n" and quote is not None:
            return None
        elif quote is None and char in ";&|\n":
            end = index + 1
            if char in "&|" and end < len(command) and command[end] == char:
                end += 1
            operator = command[index:end]
            if operator == "&":
                return None
            parts.append((command[start:index], operator))
            index = start = end
            continue
        index += 1
    if quote is not None or escaped:
        return None
    parts.append((command[start:], None))
    return parts


def literal_pinned_read_tokens(command: str, *, cwd: Path,
                               immutable_paths: tuple[Path, ...],
                               workspace: Path | None = None) -> list[str] | None:
    """Mask proven read operands, never writes, directories or nested code.

    Only no-option file reads (plus the conventional ``--`` separator) qualify.
    In particular hash check-mode, sed programs and interpreter code do not.
    Unrecognized syntax retains the existing conservative immutable hard deny.
    """
    if os.name == "nt":
        return None
    pinned = {path for path in immutable_paths if path.is_file()}
    if not pinned:
        return None
    workspace = workspace or cwd
    try:
        outer = shlex.split(command, comments=False, posix=True)
        if (len(outer) == 3 and outer[1] in {"-c", "-lc"}
                and _system_command(outer[0], _SHELLS, workspace=workspace)):
            command = outer[2]
        if any(char in command for char in "$`(){}") or "<<" in command:
            return None
        parts = _literal_parts(command)
        if parts is None:
            return None
        segments = [(shlex.split(text, comments=False, posix=True), operator) for text, operator in parts]
    except (OSError, RuntimeError, ValueError):
        return None
    if any(tokens and (
        "=" in tokens[0]
        or tokens[0] == "."
        or any(Path(token).name in _SHELL_STATE_COMMANDS | _SHELLS
               | {"fish", "python", "python3", "node", "perl", "ruby", "awk", "xargs", "env", "sudo", "doas"}
               for token in tokens)
    ) for tokens, _ in segments):
        return None

    masked: list[str] = []
    changed = False
    previous_operator = None
    for segment, operator in segments:
        start = len(masked)
        masked.extend(segment)
        if operator is not None:
            masked.append(operator)
        receives_pipe = previous_operator == "|"
        previous_operator = operator
        if (not segment or not _system_command(segment[0], _READERS, workspace=workspace) or len(segment) < 2
                or receives_pipe or operator == "|"
                or any(any(char in token for char in "<>#") for token in segment)):
            continue
        operands = segment[1:]
        if operands and operands[0] == "--":
            operands = operands[1:]
        if not operands or any(token.startswith("-") for token in operands):
            continue
        for offset, token in enumerate(segment[1:], 1):
            if token == "--" or any(char in token for char in "*?[]\r\n"):
                continue
            try:
                candidate = Path(token)
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if canonical in pinned:
                masked[start + offset] = "__bello_verified_pinned_file_read__"
                changed = True
    return masked if changed else None
