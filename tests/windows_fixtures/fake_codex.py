"""Deterministic Codex CLI stand-in used only by the native Windows CI smoke."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REQUIRED_SCHEMA_FILES = (
    "ClientRequest.json",
    "ServerRequest.json",
    "TurnStartParams.json",
    "CommandExecutionRequestApprovalParams.json",
)


def _generate_schema(args: list[str]) -> int:
    try:
        output = Path(args[args.index("--out") + 1])
    except (ValueError, IndexError):
        print("missing --out", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_SCHEMA_FILES:
        (output / name).write_text('{"type":"object"}\n', encoding="utf-8")
    return 0


def _serve_app_server() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return 2
        request_id = request.get("id")
        if request_id is None:
            continue
        method = request.get("method")
        if method == "account/read":
            result = {
                "requiresOpenaiAuth": True,
                "account": {"type": "chatgpt", "email": "ci@example.invalid"},
            }
        else:
            result = {}
        print(json.dumps({"id": request_id, "result": result}), flush=True)
    return 0


def main(args: list[str]) -> int:
    if args == ["--version"]:
        print("codex-cli 0.0.0-ci")
        return 0
    if args[:2] == ["app-server", "--help"]:
        print("Deterministic Codex app-server fixture")
        return 0
    if args[:2] == ["app-server", "generate-json-schema"]:
        return _generate_schema(args[2:])
    if args and args[0] == "app-server":
        return _serve_app_server()
    print("unsupported fake Codex invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
