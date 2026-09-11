"""Small stdlib-only filesystem worker, executed *inside* the OS sandbox.

This is not a host-side permission boundary. The enclosing sandbox owns that
boundary, including symlink races and writes attempted by arbitrary commands.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def _frame_markers(nonce: str, name: str) -> tuple[str, str]:
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("invalid file-worker response identifier")
    if name not in {"read_file", "list_directory", "search", "view_image", "write_file", "edit_file"}:
        raise ValueError("unknown filesystem operation")
    return f"\x1eBELLO_FILE_V1:{nonce}:{name}:", f"\x1fBELLO_FILE_END:{nonce}\x1e"


def frame_response(nonce: str, name: str, value: dict) -> str:
    start, end = _frame_markers(nonce, name)
    # ASCII JSON escapes control characters, so file contents cannot introduce
    # protocol delimiters. It also works under Windows' legacy stdout encoding.
    return start + json.dumps(value, ensure_ascii=True, allow_nan=False) + end


def parse_response(output: str, nonce: str, name: str) -> tuple[dict, str]:
    """Decode only our invocation's frame; retain interpreter diagnostics.

    stderr and stdout share the sandbox's output pipe. A startup warning is not
    a result, and scanning for the first JSON object could mistake it for one.
    This framing is not an authentication or filesystem-security boundary.
    """
    start, end = _frame_markers(nonce, name)
    if output.count(start) != 1 or output.count(end) != 1:
        raise ValueError("file-worker response frame is missing or duplicated")
    before, _, tail = output.partition(start)
    body, _, after = tail.partition(end)
    if end in before or not body or "\n" in body or "\r" in body:
        raise ValueError("file-worker response frame is malformed")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("file-worker response frame contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("file-worker response must be an object")
    return value, (before + after).strip()


def operate(name: str, args: dict) -> dict:
    path = Path(args["path"])
    if name == "view_image":
        with path.open("rb") as stream:
            data = stream.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("image exceeds the 5 MiB tool limit; provide a smaller image")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("unsupported image format; use PNG, JPEG, GIF, or WebP")
        return {"data": base64.b64encode(data).decode("ascii"), "mimeType": mime, "size": len(data)}
    if name == "read_file":
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        offset = args.get("offset", 1)
        limit = args.get("limit", 200)
        selected = lines[offset - 1:offset - 1 + limit]
        return {"text": "".join(f"{number}: {line}" for number, line in enumerate(selected, offset)),
                "total_lines": len(lines), "offset": offset, "returned_lines": len(selected)}
    if name == "list_directory":
        return {"entries": [{"name": entry.name, "type": "symlink" if entry.is_symlink()
                else "directory" if entry.is_dir() else "file"} for entry in sorted(path.iterdir())]}
    if name == "search":
        pattern = re.compile(args["pattern"])
        files = [path] if path.is_file() else path.rglob("*")
        matches, errors = [], []
        for item in files:
            if not item.is_file() or item.is_symlink():
                continue
            try:
                for line, text in enumerate(item.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(text):
                        matches.append({"path": str(item), "line": line, "text": text})
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                errors.append({"path": str(item), "error": str(exc)})
        return {"matches": matches, "errors": errors}
    if name not in {"write_file", "edit_file"}:
        raise ValueError("unknown filesystem operation")
    if path.is_symlink():
        raise ValueError("edits must target a regular file, not a symbolic link")
    if name == "edit_file":
        original = path.read_text(encoding="utf-8")
        old = args["old_text"]
        if not old:
            raise ValueError("old_text must be nonempty")
        count = original.count(old)
        if count == 0 or (count > 1 and not args.get("replace_all", False)):
            raise ValueError(f"old_text has {count} matches; supply an unambiguous edit")
        content = original.replace(old, args["new_text"])
    else:
        content = args["content"]
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".bello-edit-", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}


def main() -> int:
    # Validate the trusted caller's frame identifier before any filesystem action.
    request = json.loads(base64.b64decode(sys.argv[1], validate=True))
    nonce, name = request["response_nonce"], request["name"]
    _frame_markers(nonce, name)
    try:
        result = operate(name, request["arguments"])
        code = 0
    except Exception as exc:
        result, code = {"error": str(exc)}, 1
    print(frame_response(nonce, name, result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
