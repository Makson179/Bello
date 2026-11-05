"""Historical inspection labels from existing edit metadata, never a delivery gate.

No file reads, content hashes, proof checks, or model-call side effects.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path, PureWindowsPath
from typing import Any, Literal


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _relative_path(root: Path, value: Any, *, base: Path | None = None) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid_path")
    path = Path(value)
    if ".." in path.parts or (os.name != "nt" and PureWindowsPath(value).drive):
        raise ValueError("outside_workspace")
    absolute = Path(os.path.abspath(path if path.is_absolute() else (base or root) / path))
    return absolute.relative_to(root)


def inspection_staleness(
    inspection: Any, changed_files: Iterable[Any], *, workspace_root: Path,
) -> Literal["stale", "unknown", "historical"]:
    """Label an old read without asserting that its contents remain current."""
    sequence = _field(inspection, "sequence")
    paths = _field(inspection, "inspected_paths", [])
    if (not _field(inspection, "passed", False) or not isinstance(sequence, int)
            or isinstance(sequence, bool) or sequence < 0 or not paths):
        return "unknown"
    root = Path(os.path.abspath(workspace_root))
    try:
        cwd = _field(inspection, "cwd")
        base = root / _relative_path(root, cwd) if cwd else root
        inspected = [_relative_path(root, path, base=base) for path in paths]
        unknown = False
        for change in changed_files:
            changed = _relative_path(root, _field(change, "path"))
            if not any(changed == path or path in changed.parents or changed in path.parents for path in inspected):
                continue
            edit_sequence = _field(change, "sequence")
            if not isinstance(edit_sequence, int) or isinstance(edit_sequence, bool) or edit_sequence < 0:
                unknown = True
            elif edit_sequence > sequence:
                return "stale"
        return "unknown" if unknown else "historical"
    except (ValueError, TypeError):
        return "unknown"
