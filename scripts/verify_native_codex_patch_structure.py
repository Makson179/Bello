#!/usr/bin/env python3
"""Structural check of a unified-diff source patch, without applying it.

The native Codex source is not available where the helper patch is edited, so a
wrong hunk count would otherwise surface only in a native build. This checks what
`git apply` requires of the text itself: file headers that agree with each other,
exact hunk line counts, new-file shape, ordered non-overlapping hunks and
consistent new-side offsets. It cannot prove that context lines match upstream.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATCH = ROOT / "scripts" / "native-codex-0.161.0.patch"
_DIFF = "diff --git "
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_FILE = re.compile(r"^diff --git a/(.+) b/(.+)$")


@dataclass
class Hunk:
    line: int
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: list[str] = field(default_factory=list)

    @property
    def old_position(self) -> int:
        # A zero-length range names the line before it.
        return self.old_start if self.old_count else self.old_start + 1

    @property
    def new_position(self) -> int:
        return self.new_start if self.new_count else self.new_start + 1


@dataclass
class FileDiff:
    line: int
    path_a: str
    path_b: str
    extended: list[str] = field(default_factory=list)
    old_name: str | None = None
    new_name: str | None = None
    hunks: list[Hunk] = field(default_factory=list)


def parse(text: str) -> tuple[list[FileDiff], list[str]]:
    """Parse by declared counts, as git does, and report every inconsistency."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    errors: list[str] = []
    files: list[FileDiff] = []
    total, index = len(lines), 0
    while index < total and not lines[index].startswith(_DIFF):
        index += 1  # License/preamble text before the first diff.
    while index < total:
        match = _FILE.match(lines[index])
        if not lines[index].startswith(_DIFF) or match is None:
            errors.append(f"line {index + 1}: expected a 'diff --git a/X b/X' file header")
            index += 1
            continue
        current = FileDiff(index + 1, match[1], match[2])
        files.append(current)
        index += 1
        while index < total and not lines[index].startswith(("--- ", "@@ ", _DIFF)):
            current.extended.append(lines[index])
            index += 1
        if index < total and lines[index].startswith("--- "):
            current.old_name = lines[index][4:]
            index += 1
            if index < total and lines[index].startswith("+++ "):
                current.new_name = lines[index][4:]
                index += 1
            else:
                errors.append(f"line {index + 1}: '---' header is not followed by '+++'")
        while index < total and lines[index].startswith("@@ "):
            header = _HUNK.match(lines[index])
            if header is None:
                errors.append(f"line {index + 1}: malformed hunk header")
                index += 1
                continue
            old_start, old_count, new_start, new_count = (
                int(header[1]), int(header[2] or 1), int(header[3]), int(header[4] or 1))
            hunk = Hunk(index + 1, old_start, old_count, new_start, new_count)
            current.hunks.append(hunk)
            index += 1
            old_left, new_left = old_count, new_count
            while old_left > 0 or new_left > 0:
                if index >= total:
                    errors.append(f"line {hunk.line}: hunk is truncated by the end of the patch")
                    break
                text_line = lines[index]
                if text_line.startswith("\\"):
                    hunk.body.append(text_line)
                    index += 1
                    continue
                marker = text_line[:1]
                if marker in (" ", ""):  # A bare empty line is an empty context line.
                    old_left, new_left = old_left - 1, new_left - 1
                elif marker == "-":
                    old_left -= 1
                elif marker == "+":
                    new_left -= 1
                else:
                    errors.append(f"line {index + 1}: hunk at line {hunk.line} declares more lines than it has")
                    break
                if old_left < 0 or new_left < 0:
                    errors.append(f"line {index + 1}: hunk at line {hunk.line} has more lines than declared")
                    break
                hunk.body.append(text_line)
                index += 1
            while index < total and lines[index].startswith("\\"):
                hunk.body.append(lines[index])
                index += 1
            if index < total and not lines[index].startswith(("@@ ", _DIFF)):
                errors.append(f"line {index + 1}: text after hunk at line {hunk.line} (declared counts too small?)")
                while index < total and not lines[index].startswith(("@@ ", _DIFF)):
                    index += 1
        if index < total and not lines[index].startswith(_DIFF):
            errors.append(f"line {index + 1}: unexpected text inside file section at line {current.line}")
            while index < total and not lines[index].startswith(_DIFF):
                index += 1
    return files, errors


def _check_file(current: FileDiff) -> list[str]:
    where = f"{current.path_b} (line {current.line})"
    errors: list[str] = []
    is_new = any(entry.startswith("new file mode") for entry in current.extended)
    is_deleted = any(entry.startswith("deleted file mode") for entry in current.extended)
    renamed = any(entry.startswith("rename from") for entry in current.extended)
    if current.path_a != current.path_b and not renamed:
        errors.append(f"{where}: a/ and b/ paths differ without a rename")
    if current.old_name is None:
        if current.hunks:
            errors.append(f"{where}: hunks without ---/+++ file headers")
        return errors
    expected_old = "/dev/null" if is_new else "a/" + current.path_a
    expected_new = "/dev/null" if is_deleted else "b/" + current.path_b
    if current.old_name.split("\t")[0] != expected_old:
        errors.append(f"{where}: '---' header {current.old_name!r} does not match {expected_old!r}")
    if (current.new_name or "").split("\t")[0] != expected_new:
        errors.append(f"{where}: '+++' header {current.new_name!r} does not match {expected_new!r}")
    if not current.hunks:
        errors.append(f"{where}: file section has no hunks")
        return errors
    if is_new:
        first = current.hunks[0]
        if len(current.hunks) != 1 or (first.old_start, first.old_count, first.new_start) != (0, 0, 1):
            errors.append(f"{where}: a new file needs exactly one '@@ -0,0 +1,N @@' hunk")
        for hunk in current.hunks:
            if any(not entry.startswith(("+", "\\")) for entry in hunk.body):
                errors.append(f"{where}: new-file hunk at line {hunk.line} contains non-added lines")
    elif is_deleted:
        if len(current.hunks) != 1 or (current.hunks[0].new_start, current.hunks[0].new_count) != (0, 0):
            errors.append(f"{where}: a deleted file needs exactly one '@@ -1,N +0,0 @@' hunk")
    else:
        delta, previous_end = 0, 0
        for hunk in current.hunks:
            if hunk.old_position < previous_end:
                errors.append(f"{where}: hunk at line {hunk.line} overlaps or precedes the previous hunk")
            if hunk.new_position - hunk.old_position != delta:
                errors.append(
                    f"{where}: hunk at line {hunk.line} starts at new line {hunk.new_start}; "
                    f"preceding hunks require {hunk.old_position + delta - (hunk.new_position - hunk.new_start)}")
            changed = any(entry[:1] in ("+", "-") for entry in hunk.body)
            if not changed:
                errors.append(f"{where}: hunk at line {hunk.line} changes nothing")
            delta += hunk.new_count - hunk.old_count
            previous_end = hunk.old_position + hunk.old_count
    return errors


def check_text(text: str) -> list[str]:
    files, errors = parse(text)
    if not files:
        errors.append("the patch contains no 'diff --git' file sections")
    seen: dict[str, int] = {}
    for current in files:
        if current.path_b in seen:
            errors.append(f"{current.path_b} (line {current.line}): file section repeats line {seen[current.path_b]}")
        seen.setdefault(current.path_b, current.line)
        errors.extend(_check_file(current))
    return errors


def check(path: Path) -> list[str]:
    # Match preparation: CRLF checkouts are normalized before `git apply`.
    return check_text(path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patch", nargs="?", type=Path, default=DEFAULT_PATCH)
    problems = check(parser.parse_args(argv).patch)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
