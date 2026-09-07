from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from supervisor.appserver import AppServerError
from supervisor.runtime.claude import ClaudeBackend, SUPPORTED_EFFORTS
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.tools import TOOL_DEFINITIONS, ToolHost, ToolScope, tool_result


AUTH = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max",
    # The backend must discard identity rather than persisting or returning it.
    "email": "private@example.test",
}
MODELS = [
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5",
        "displayName": "Sonnet",
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    }
]
TOOLS = [
    {
        "name": "read_file",
        "description": "Read one file through Bello.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }
]


def result_message(
    *,
    session_id: str = "11111111-1111-4111-8111-111111111111",
    result: str | None = "done",
    structured_output=None,
    is_error: bool = False,
    terminal_reason: str = "completed",
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=5,
        duration_api_ms=3,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        result=result,
        structured_output=structured_output,
        usage={"input_tokens": 7, "output_tokens": 3},
        terminal_reason=terminal_reason,
    )


class FakeClient:
    def __init__(self, factory: "FakeFactory", options):
        self.factory = factory
        self.options = options
        self.messages: asyncio.Queue = asyncio.Queue()
        self.queries: list[str] = []
        self.interrupts = 0
        self.connect_task = None
        self.disconnect_task = None

    async def connect(self):
        self.connect_task = asyncio.current_task()
        self.factory.connected.set()
        if self.factory.connect_gate is not None:
            await self.factory.connect_gate.wait()

    async def disconnect(self):
        self.disconnect_task = asyncio.current_task()

    async def get_server_info(self):
        return {
            "account": {"apiProvider": self.factory.api_provider},
            "models": self.factory.models,
        }

    async def get_mcp_status(self):
        return {"mcpServers": list(self.factory.mcp_servers)}

    async def query(self, prompt: str):
        self.queries.append(prompt)
        if self.factory.complete_after_queries == len(self.queries):
            for message in self.factory.messages:
                await self.messages.put(message)

    async def receive_response(self):
        while True:
            message = await self.messages.get()
            yield message
            if isinstance(message, ResultMessage):
                return

    async def interrupt(self):
        self.interrupts += 1
        if self.factory.complete_on_interrupt:
            await self.messages.put(
                result_message(result=None, terminal_reason="aborted_streaming")
            )


class FakeFactory:
    def __init__(
        self,
        messages=(),
        *,
        models=None,
        complete_after_queries: int | None = 1,
        complete_on_interrupt: bool = False,
        api_provider: str = "firstParty",
        mcp_servers=(),
        connect_gate: asyncio.Event | None = None,
    ):
        self.messages = list(messages)
        self.models = list(MODELS if models is None else models)
        self.complete_after_queries = complete_after_queries
        self.complete_on_interrupt = complete_on_interrupt
        self.api_provider = api_provider
        self.mcp_servers = list(mcp_servers)
        self.connect_gate = connect_gate
        self.clients: list[FakeClient] = []
        self.connected = asyncio.Event()

    def __call__(self, options):
        client = FakeClient(self, options)
        self.clients.append(client)
        return client


def backend(tmp_path: Path, factory: FakeFactory, events: list[dict], tool_handler=None, auth=AUTH):
    async def emit(event):
        events.append(event)

    async def default_tool(_request):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    return ClaudeBackend(
        tmp_path / "claude-state",
        emit,
        tool_handler=tool_handler or default_tool,
        client_factory=factory,
        auth_probe=lambda: dict(auth),
        environment={},
    )


async def start_thread(instance: ClaudeBackend, tmp_path: Path, *, effort="high") -> None:
    await instance.request("initialize", {})
    response = await instance.request(
        "thread/start",
        {
            "threadId": "thread-1",
            "provider": "claude-code",
            "model": "sonnet",
            "cwd": str(tmp_path),
            "tools": TOOLS,
            "effort": effort,
            "developerInstructions": "Keep responses concise.",
        },
    )
    assert response["thread"]["id"] == "thread-1"


async def wait_completed(events: list[dict]) -> dict:
    async with asyncio.timeout(2):
        while True:
            for event in events:
                if event["method"] == "turn/completed":
                    return event
            await asyncio.sleep(0)


async def wait_disconnected(client: FakeClient) -> None:
    async with asyncio.timeout(2):
        while client.disconnect_task is None:
            await asyncio.sleep(0)


async def test_subscription_auth_rejects_environment_provider_routes_without_leaking_values(
    tmp_path: Path,
) -> None:
    called = False

    def probe():
        nonlocal called
        called = True
        return AUTH

    instance = ClaudeBackend(
        tmp_path / "state",
        lambda _event: None,
        tool_handler=lambda _request: None,
        client_factory=FakeFactory(),
        auth_probe=probe,
        environment={"ANTHROPIC_API_KEY": "super-secret", "CLAUDE_CODE_USE_VERTEX": "1"},
    )
    with pytest.raises(AppServerError) as exc_info:
        await instance.request("initialize", {})
    text = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in text
    assert "CLAUDE_CODE_USE_VERTEX" in text
    assert "super-secret" not in text
    assert called is False


@pytest.mark.parametrize(
    "auth",
    [
        {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty", "subscriptionType": "max"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock", "subscriptionType": "max"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"},
    ],
)
async def test_initialize_requires_existing_first_party_paid_subscription(tmp_path: Path, auth) -> None:
    instance = backend(tmp_path, FakeFactory(), [], auth=auth)
    with pytest.raises(AppServerError):
        await instance.request("initialize", {})


async def test_secure_sdk_options_and_streamed_normalized_events(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory(
        [
            AssistantMessage(content=[TextBlock("hello "), TextBlock("world")], model="sonnet"),
            result_message(),
        ]
    )
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    response = await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-1", "input": [{"type": "text", "text": "work"}]},
    )
    assert response["turn"]["id"] == "turn-1"
    assert response["turn"]["status"] == "inProgress"
    completed = await wait_completed(events)
    turn = completed["params"]["turn"]
    assert turn["status"] == "completed"
    assert turn["items"][-1]["text"] == "hello world"
    assert turn["usage"] == {"input_tokens": 7, "output_tokens": 3}

    client = factory.clients[-1]
    options = client.options
    assert options.tools == []
    assert options.setting_sources == []
    assert options.skills == [] and options.plugins == [] and options.hooks is None
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.fallback_model is None
    assert options.model == "sonnet" and options.effort == "high"
    assert "Agent" in options.disallowed_tools and "Task" in options.disallowed_tools
    assert options.allowed_tools == ["mcp__bello__read_file"]
    assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    settings = json.loads(options.settings)
    assert settings == {"autoMemoryEnabled": False, "disableAllHooks": True, "enabledPlugins": {}}
    assert options.extra_args == {"disable-slash-commands": None, "no-chrome": None}
    assert "Keep responses concise." in options.system_prompt
    await wait_disconnected(client)
    assert client.connect_task is client.disconnect_task
    await instance.stop()


async def test_sdk_mcp_protocol_tool_use_id_preserves_host_at_most_once(tmp_path: Path) -> None:
    executions: list[dict] = []
    routed: list[dict] = []
    journal = RuntimeJournal(tmp_path / "journal")

    async def approve(*_args):
        raise AssertionError("a delegated call must not ask for command approval")

    async def emit(_message):
        return None

    async def delegate(_name, arguments, _thread_id, _turn_id):
        executions.append(arguments)
        return tool_result("child queued")

    host = ToolHost(
        journal,
        lambda _thread_id, _turn_id: ToolScope(tmp_path, "workspace-write"),
        approve,
        emit,
        delegate,
    )

    async def tool_handler(request):
        routed.append(request)
        return await host.call(request)

    instance = backend(tmp_path, FakeFactory(), [], tool_handler=tool_handler)
    bridge = None
    try:
        await start_thread(instance, tmp_path)
        record = instance._record("thread-1")
        record["activeTurnId"] = "turn-7"
        record["tools"] = [
            next(definition for definition in TOOL_DEFINITIONS if definition["name"] == "spawn_agent")
        ]
        config = instance._sdk_server(record)
        bridge = SdkMcpBridge("bello", config["instance"])
        await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": "initialize-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        await bridge.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

        arguments = {"message": "inspect", "model": "sonnet", "effort": "high"}

        async def call(request_id: str, tool_use_id: str):
            return await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "spawn_agent",
                        "arguments": arguments,
                        "_meta": {"claudecode/toolUseId": tool_use_id},
                    },
                }
            )

        first = await call("wire-1", "toolu_stable")
        replay = await call("wire-2", "toolu_stable")
        distinct = await call("wire-3", "toolu_distinct")

        assert first["result"] == replay["result"]
        assert distinct["result"] == first["result"]
        assert len(routed) == 3
        assert routed[0]["callId"] == routed[1]["callId"]
        assert routed[2]["callId"] != routed[0]["callId"]
        assert executions == [arguments, arguments]
        assert all(request["threadId"] == "thread-1" for request in routed)
        assert all(request["turnId"] == "turn-7" for request in routed)
    finally:
        if bridge is not None:
            await bridge.aclose()
        await instance.stop()
        journal.close()


async def test_output_schema_is_forwarded_revalidated_and_canonicalized(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory([result_message(result='{"answer":1}', structured_output={"answer": 1})])
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    await instance.request(
        "turn/start",
        {
            "threadId": "thread-1",
            "turnId": "turn-structured",
            "input": [{"type": "text", "text": "answer"}],
            "outputSchema": schema,
        },
    )
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "completed"
    assert completed["params"]["turn"]["items"][-1]["text"] == '{"answer":1}'
    assert factory.clients[-1].options.output_format == {"type": "json_schema", "schema": schema}
    await instance.stop()


async def test_invalid_structured_result_fails_closed(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory([result_message(result="{}", structured_output={})])
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.request(
        "turn/start",
        {
            "threadId": "thread-1",
            "turnId": "turn-invalid",
            "input": [{"type": "text", "text": "answer"}],
            "outputSchema": {"type": "object", "required": ["answer"]},
        },
    )
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "failed"
    assert "outputSchema" in completed["params"]["turn"]["error"]["message"]
    await instance.stop()


async def test_steer_and_interrupt_are_owned_by_same_sdk_session_task(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory(
        [AssistantMessage(content=[TextBlock("steered")], model="sonnet"), result_message()],
        complete_after_queries=2,
    )
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-steer", "input": [{"type": "text", "text": "one"}]},
    )
    await asyncio.wait_for(factory.connected.wait(), 1)
    response = await instance.request(
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-steer",
            "input": [{"type": "text", "text": "two"}],
        },
    )
    assert response["turn"]["id"] == "turn-steer"
    await wait_completed(events)
    assert factory.clients[-1].queries == ["one", "two"]
    await wait_disconnected(factory.clients[-1])
    assert factory.clients[-1].connect_task is factory.clients[-1].disconnect_task
    await instance.stop()


async def test_interrupt_completes_active_turn(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory(complete_after_queries=None, complete_on_interrupt=True)
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-stop", "input": [{"type": "text", "text": "wait"}]},
    )
    await asyncio.wait_for(factory.connected.wait(), 1)
    await instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-stop"})
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "interrupted"
    assert factory.clients[-1].interrupts == 1
    await instance.stop()


async def test_interrupt_during_slow_connect_prevents_model_query(tmp_path: Path) -> None:
    events: list[dict] = []
    gate = asyncio.Event()
    factory = FakeFactory(complete_after_queries=None, connect_gate=gate)
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-1", "input": [{"type": "text", "text": "work"}]},
    )
    await asyncio.wait_for(factory.connected.wait(), 1)
    interrupt = asyncio.create_task(
        instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"})
    )
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(interrupt, 1)
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "interrupted"
    assert factory.clients[-1].queries == []
    await instance.stop()


async def test_model_catalog_exposes_resolved_ids_and_exact_efforts(tmp_path: Path) -> None:
    factory = FakeFactory()
    instance = backend(tmp_path, factory, [])
    await instance.request("initialize", {})
    response = await instance.request("model/list", {})
    by_id = {entry["id"]: entry for entry in response["data"]}
    assert set(by_id) == {"sonnet", "claude-sonnet-5"}
    assert by_id["sonnet"]["qualifiedId"] == "claude-code/sonnet"
    assert by_id["sonnet"]["supportedReasoningEfforts"] == list(SUPPORTED_EFFORTS)
    assert by_id["sonnet"]["billingRoute"] == "subscription"
    assert factory.clients[-1].connect_task is factory.clients[-1].disconnect_task
    await instance.stop()


async def test_model_validate_checks_exact_subscription_profile_without_query(tmp_path: Path) -> None:
    factory = FakeFactory()
    instance = backend(tmp_path, factory, [])
    result = await instance.request(
        "model/validate",
        {"provider": "claude-code", "model": "sonnet", "effort": "max"},
    )
    assert result == {
        "valid": True,
        "model": {
            "id": "sonnet",
            "model": "sonnet",
            "qualifiedId": "claude-code/sonnet",
            "provider": "claude-code",
            "name": "Sonnet",
            "displayName": "Sonnet",
            "resolvedModel": "claude-sonnet-5",
            "available": True,
            "configured": True,
            "reasoning": True,
            "supportedEfforts": ["low", "medium", "high", "xhigh", "max"],
            "supportedReasoningEfforts": ["low", "medium", "high", "xhigh", "max"],
            "supportsServiceTier": False,
            "billingRoute": "subscription",
        },
        "requested": {"effort": "max", "serviceTier": None},
        "execution": {"engine": "claude-code", "effort": "max"},
    }
    assert len(factory.clients) == 1
    assert factory.clients[0].queries == []
    await instance.stop()


@pytest.mark.parametrize(
    "params,match",
    [
        ({"provider": "anthropic", "model": "sonnet"}, "provider 'claude-code'"),
        ({"provider": "claude-code", "model": "missing"}, "did not advertise model"),
        (
            {"provider": "claude-code", "model": "sonnet", "effort": "ultra"},
            "not supported",
        ),
        (
            {"provider": "claude-code", "model": "sonnet", "serviceTier": "priority"},
            "service tier",
        ),
    ],
)
async def test_model_validate_rejects_route_model_effort_and_tier_mismatch(
    tmp_path: Path, params: dict, match: str
) -> None:
    factory = FakeFactory()
    instance = backend(tmp_path, factory, [])
    with pytest.raises(AppServerError, match=match):
        await instance.request("model/validate", params)
    assert all(client.queries == [] for client in factory.clients)
    await instance.stop()


async def test_public_thread_status_and_effort_follow_turn_lifecycle(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory(complete_after_queries=None, complete_on_interrupt=True)
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path, effort="xhigh")
    idle = await instance.request("thread/read", {"threadId": "thread-1", "includeTurns": False})
    assert idle["thread"]["model"] == "claude-code/sonnet"
    assert idle["thread"]["reasoningEffort"] == "xhigh"
    assert idle["thread"]["status"] == {"type": "idle"}
    await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-status", "input": [{"type": "text", "text": "wait"}]},
    )
    active = await instance.request("thread/read", {"threadId": "thread-1", "includeTurns": False})
    assert active["thread"]["status"] == {"type": "active"}
    await asyncio.wait_for(factory.connected.wait(), 1)
    await instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-status"})
    await wait_completed(events)
    idle_again = await instance.request("thread/read", {"threadId": "thread-1", "includeTurns": False})
    assert idle_again["thread"]["status"] == {"type": "idle"}
    await instance.stop()


async def test_unadvertised_effort_and_ultra_are_never_substituted(tmp_path: Path) -> None:
    events: list[dict] = []
    no_effort_models = [{"value": "sonnet", "resolvedModel": "claude-sonnet-5"}]
    instance = backend(tmp_path, FakeFactory([result_message()], models=no_effort_models), events)
    await start_thread(instance, tmp_path, effort=None)
    with pytest.raises(AppServerError, match="ultra"):
        await instance.request(
            "turn/start",
            {
                "threadId": "thread-1",
                "turnId": "turn-ultra",
                "effort": "ultra",
                "input": [{"type": "text", "text": "work"}],
            },
        )
    await instance.request(
        "turn/start",
        {
            "threadId": "thread-1",
            "turnId": "turn-high",
            "effort": "high",
            "input": [{"type": "text", "text": "work"}],
        },
    )
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "failed"
    assert "does not advertise effort" in completed["params"]["turn"]["error"]["message"]
    await instance.stop()


async def test_foreign_managed_mcp_fails_before_query(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory(
        [result_message()],
        mcp_servers=[{"name": "corporate-shell", "status": "connected", "scope": "managed"}],
    )
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.request(
        "turn/start",
        {"threadId": "thread-1", "turnId": "turn-1", "input": [{"type": "text", "text": "work"}]},
    )
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "failed"
    assert "single ToolHost boundary" in completed["params"]["turn"]["error"]["message"]
    assert factory.clients[-1].queries == []
    await instance.stop()


async def test_thread_state_resumes_without_storing_identity_or_credentials(tmp_path: Path) -> None:
    events: list[dict] = []
    factory = FakeFactory()
    instance = backend(tmp_path, factory, events)
    await start_thread(instance, tmp_path)
    await instance.stop()

    state_text = (tmp_path / "claude-state" / "threads.json").read_text(encoding="utf-8")
    assert "private@example.test" not in state_text
    assert "super-secret" not in state_text

    resumed = backend(tmp_path, FakeFactory(), [])
    await resumed.request("initialize", {})
    response = await resumed.request("thread/resume", {"threadId": "thread-1", "model": "sonnet", "tools": TOOLS})
    assert response["thread"]["id"] == "thread-1"
    assert response["thread"]["model"] == "claude-code/sonnet"
    await resumed.request("thread/archive", {"threadId": "thread-1"})
    read = await resumed.request("thread/read", {"threadId": "thread-1", "includeTurns": True})
    assert read["thread"]["status"] == {"type": "archived"}
    await resumed.stop()


async def test_text_input_and_tool_contract_fail_closed(tmp_path: Path) -> None:
    instance = backend(tmp_path, FakeFactory(), [])
    await start_thread(instance, tmp_path)
    with pytest.raises(AppServerError, match="text input only"):
        await instance.request(
            "turn/start",
            {"threadId": "thread-1", "turnId": "turn-image", "input": [{"type": "image", "url": "x"}]},
        )
    await instance.stop()
