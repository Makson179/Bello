from types import SimpleNamespace
import asyncio

from supervisor.runtime.claude import ClaudeBackend


def test_control_diagnostic_contains_only_counters():
    receiver = SimpleNamespace(statistics=lambda: SimpleNamespace(
        current_buffer_used=100, max_buffer_size=100,
        tasks_waiting_send=1, tasks_waiting_receive=0))
    query = SimpleNamespace(
        _closed=False, _initialized=False,
        pending_control_responses={"secret-request": object()},
        _inflight_requests={}, _message_receive=receiver,
        _read_task=SimpleNamespace(done=lambda: False),
        _last_error_result={"api_error_status": 429, "result": "PRIVATE CONTENT"})
    client = SimpleNamespace(_query=query, _transport=SimpleNamespace(
        _process=SimpleNamespace(returncode=None)))
    actual = ClaudeBackend._control_channel_diagnostic(client)
    assert actual == {"available": True, "closed": False, "initialized": False,
                      "pending_controls": 1, "inflight_incoming_controls": 0,
                      "current_buffer_used": 100, "max_buffer_size": 100,
                      "tasks_waiting_send": 1, "tasks_waiting_receive": 0,
                      "reader_done": False, "process_exited": False,
                      "error_result_seen": True, "api_error_status": 429}
    assert "PRIVATE" not in str(actual) and "secret" not in str(actual)


def test_snapshot_survives_sdk_disconnect_without_query():
    assert ClaudeBackend._control_channel_diagnostic(SimpleNamespace(
        _query=None, _bello_control_snapshot={"available": True, "current_buffer_used": 100}
    )) == {"available": True, "current_buffer_used": 100}


def test_diagnostic_failure_cannot_replace_original_error():
    def broken():
        raise RuntimeError("secret")
    client = SimpleNamespace(_query=SimpleNamespace(
        _message_receive=SimpleNamespace(statistics=broken)))
    assert ClaudeBackend._control_channel_diagnostic(client) == {
        "available": False, "snapshot_failed": True}


def test_real_client_snapshots_before_sdk_disconnect(monkeypatch):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    async def cleanup(self):
        self._query = None

    monkeypatch.setattr(ClaudeSDKClient, "disconnect", cleanup)
    backend = ClaudeBackend.__new__(ClaudeBackend)
    backend._client_factory = None
    client = backend._new_client(ClaudeAgentOptions())
    client._query = SimpleNamespace(_closed=False, _initialized=True)
    asyncio.run(client.disconnect())
    assert client._query is None
    assert backend._control_channel_diagnostic(client) == {
        "available": True, "closed": False, "initialized": True,
        "error_result_seen": False, "stderr_categories": {"lines": 0, "chars": 0}}


def test_stderr_keeps_only_fixed_categories_and_preserves_callback():
    from claude_agent_sdk import ClaudeAgentOptions
    backend = ClaudeBackend.__new__(ClaudeBackend)
    backend._client_factory = None
    forwarded = []
    options = ClaudeAgentOptions(stderr=forwarded.append)
    client = backend._new_client(options)
    content = "OAuth refresh failed secret-token https://example.test/?code=secret"
    options.stderr(content)
    assert forwarded == [content]
    client._query = SimpleNamespace()
    actual = backend._control_channel_diagnostic(client)
    assert actual["stderr_categories"] == {
        "lines": 1, "chars": len(content), "oauth": 1, "refresh": 1}
    assert "secret" not in str(actual) and "https" not in str(actual)


def test_missing_process_is_diagnostic_not_fatal():
    value = ClaudeBackend._child_process_diagnostic(2147483647)
    assert value == {"pid": 2147483647, "unavailable_type": "FileNotFoundError"}
