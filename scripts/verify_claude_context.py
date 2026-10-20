#!/usr/bin/env python3
"""Exercise the official SDK/CLI against a synthetic loopback-only provider.

No account credentials or paid inference are used. A fresh temporary HOME and
CLAUDE_CONFIG_DIR isolate native transcripts. The fixture substitutes only the
account metadata in get_server_info(), because a local dummy API key is not a
subscription login. Production account validation remains unchanged. All
context accounting, MCP dispatch, SDK slicing, summary requests, session
rollover and structured output use the real installed SDK and bundled CLI. The
summary request is answered with an ordinary plain-text handoff, exactly as the
summarizer is asked to write it: no JSON envelope and no required headings.

Run: python scripts/verify_claude_context.py
Output is a bounded receipt; provider payloads and transcript data stay private.
"""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_agent_sdk import ClaudeSDKClient

from supervisor.runtime.claude import ClaudeBackend
from supervisor.runtime.claude_cli import sdk_bundled_cli
from supervisor.runtime.claude_context import SUMMARY_SYSTEM

# The synthetic handoff the fixture "model" writes: plain text with a newline,
# quotes and braces that a JSON string would have had to escape.
FIXTURE_SUMMARY = (
    "The earlier fixture inspection completed successfully.\n"
    'The first file was read as "first" and its result is not needed again; {no} work is pending.'
)


def system_text(body):
    """The request's system prompt as one string (the CLI may send content blocks)."""
    system = body.get("system", "")
    if isinstance(system, list):
        return "\n".join(block.get("text", "") for block in system if isinstance(block, dict))
    return system if isinstance(system, str) else ""


class FixtureState:
    def __init__(self):
        self.main_requests = 0
        self.summary_requests = 0
        self.token_requests = 0
        self.tail_preserved = False
        self.old_content_absent = False
        self.structured_requests = 0

    def answer(self, body):
        if SUMMARY_SYSTEM in system_text(body):
            self.summary_requests += 1
            return {"type": "text", "text": FIXTURE_SUMMARY}
        self.main_requests += 1
        if self.main_requests <= 2:
            name = "first" if self.main_requests == 1 else "second"
            return {"type": "tool_use", "id": f"fixture-{name}", "name": "mcp__bello__read_file", "input": {"path": name}}
        history = json.dumps(body.get("messages", []))
        self.tail_preserved = "fixture-second-complete" in history
        self.old_content_absent = "fixture-first-complete" not in history
        if any(tool.get("name") == "StructuredOutput" for tool in body.get("tools", [])):
            self.structured_requests += 1
            return {"type": "tool_use", "id": "fixture-verdict", "name": "StructuredOutput", "input": {"status": "done"}}
        return {"type": "text", "text": "Fixture completed."}


def fixture_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.split("?")[0].endswith("/count_tokens"):
                state.token_requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":100}')
                return
            if self.path.split("?")[0] != "/v1/messages":
                self.send_error(404)
                return
            block = state.answer(body)
            usage = {"input_tokens": 5000, "cache_read_input_tokens": 4000,
                     "cache_creation_input_tokens": 3000, "output_tokens": 20}
            stop_reason = "tool_use" if block["type"] == "tool_use" else "end_turn"
            message = {"id": f"fixture-message-{state.main_requests}-{state.summary_requests}",
                       "type": "message", "role": "assistant", "model": body["model"],
                       "content": [block], "stop_reason": stop_reason,
                       "stop_sequence": None, "usage": usage}
            if not body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(message).encode())
                return
            start = {**block, "input": {}} if block["type"] == "tool_use" else {"type": "text", "text": ""}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]}
            events = [
                ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 0}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 20}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event, value in events:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(value)}\n\n".encode())
                self.wfile.flush()

    return Handler


def fixture_environment(directory, port):
    env = {name: "" for name in (
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "ANTHROPIC_FEDERATION_RULE_ID",
    )}
    env.update({name: "0" for name in (
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    )})
    env.update({"HOME": str(directory / "home"), "CLAUDE_CONFIG_DIR": str(directory / "config"),
                "ANTHROPIC_API_KEY": "local-fixture-not-a-credential",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "127.0.0.1,localhost",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1"})
    return env


async def verify(directory, server, state):
    events = []
    calls = []
    clients = []
    counts = []
    env = fixture_environment(directory, server.server_port)
    for name in ("home", "config"):
        (directory / name).mkdir()

    class FixtureClient(ClaudeSDKClient):
        async def get_server_info(self):
            info = dict(await super().get_server_info())
            info["account"] = {"apiProvider": "firstParty"}
            return info

        async def get_context_usage(self):
            result = await super().get_context_usage()
            counts.append(result["totalTokens"])
            return result

    def factory(options):
        options.env = {**options.env, **env}
        client = FixtureClient(options)
        clients.append(client)
        return client

    async def emit(event):
        events.append(event)

    async def tool(request):
        calls.append(request["arguments"]["path"])
        return {"content": [{"type": "text", "text": f"fixture-{calls[-1]}-complete"}], "isError": False}

    backend = ClaudeBackend(directory / ".supervisor" / "claude", emit, tool_handler=tool,
        client_factory=factory, environment={}, cli_path=sdk_bundled_cli(),
        auth_probe=lambda: {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"})
    try:
        await backend.request("initialize")
        await backend.request("thread/start", {"threadId": "fixture", "model": "sonnet", "effort": "high",
            "cwd": str(directory), "developerInstructions": "Keep the fixture scope.",
            # Below the 12,000 context tokens every fixture response reports, so the
            # second round triggers a checkpoint, and above a fresh session's
            # baseline plus bootstrap, so the checkpoint really reduces the context.
            "noNeedForContext": {"enabled": True, "thresholdTokens": 8000, "retainPercent": 10},
            "tools": [{"name": "read_file", "description": "Read harmless fixture evidence.", "parameters": {
                "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}]})
        original = backend._record("fixture")["claudeSessionId"]
        await backend.request("turn/start", {"threadId": "fixture", "turnId": "outer",
            "input": [{"type": "text", "text": "Inspect both fixture files and return status done."}],
            "outputSchema": {"type": "object", "properties": {"status": {"type": "string", "const": "done"}}, "required": ["status"], "additionalProperties": False}})
        async with asyncio.timeout(45):
            while not any(event["method"] == "turn/completed" for event in events):
                await asyncio.sleep(0.01)
        completed = next(event for event in events if event["method"] == "turn/completed")
        record = backend._record("fixture")
        assert completed["params"]["turn"]["status"] == "completed", "logical turn did not complete"
        assert calls == ["first", "second"], "MCP calls were lost or replayed"
        assert state.main_requests == 3 and state.summary_requests == 1, "unexpected inference count"
        assert state.tail_preserved and state.old_content_absent, "checkpoint did not replace older context"
        assert state.structured_requests == 1, "structured verdict was not validated"
        assert record["claudeSessionId"] != original and record["contextState"]["generation"] == 1
        assert record["contextState"]["summary"] == FIXTURE_SUMMARY, "the plain-text handoff was not kept as written"
        assert len(list((backend.state_dir / "context-archives").glob("*.json"))) == 1
        assert max(counts) >= 12_000, "cache token accounting was omitted"
        assert len([event for event in events if event["method"] == "turn/completed"]) == 1
        return {"passed": True, "synthetic_local_auth": True, "paid_provider_requests": 0,
                "main_inferences": state.main_requests, "summary_inferences": state.summary_requests,
                "tool_executions": len(calls), "checkpoint_generations": 1,
                "structured_verdict": True, "whole_tail_preserved": True,
                "plain_text_summary": True,
                "older_payload_removed": True, "cache_tokens_counted": True,
                "count_tokens_requests": state.token_requests}
    finally:
        await backend.stop()


def main():
    state = FixtureState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="bello-claude-context-") as temporary:
            receipt = asyncio.run(verify(Path(temporary), server, state))
        print(json.dumps(receipt, sort_keys=True))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
