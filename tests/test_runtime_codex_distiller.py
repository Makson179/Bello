from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import secrets
from types import SimpleNamespace

import pytest

from supervisor.runtime import codex_distiller as module


class Selector:
    def __init__(self, result="selected\n"):
        self.result = result
        self.calls = []
        self.closed = False

    async def distill(self, *args):
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self):
        self.closed = True


async def exchange_over_socket(bridge, *, command="pytest -q", focus="Check test failure", log="long original test log\n"):
    reader, writer = await asyncio.open_unix_connection(bridge.environment["BELLO_SELECTOR_SOCKET"])
    writer.write(json.dumps({"command": command, "focus": focus, "log": log}).encode() + b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


async def exchange(bridge, *, command="pytest -q", focus="Check test failure", log="long original test log\n"):
    """Exercise the actual wire handler without requiring a Unix transport."""
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps({"command": command, "focus": focus, "log": log}).encode() + b"\n")
    reader.feed_eof()

    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            assert self.closed

    writer = Writer()
    await bridge._handle(reader, writer)
    assert writer.closed
    assert len(writer.data.splitlines()) == 1
    return json.loads(writer.data)


@pytest.fixture
def native_binary(tmp_path):
    # Windows executable discovery requires PATHEXT, even with an absolute path.
    binary = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    binary.write_bytes(b"fixture native binary")
    binary.chmod(0o700)
    return binary


@pytest.mark.skipif(os.name == "nt", reason="Integration with the POSIX-only native selection transport")
async def test_private_socket_output_only_protocol_and_cleanup(tmp_path):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path / "state", tmp_path / "workspace")
    with pytest.raises(RuntimeError, match="not started"):
        _ = bridge.environment
    await bridge.start()
    socket = Path(bridge.environment["BELLO_SELECTOR_SOCKET"])
    try:
        assert socket.is_absolute()
        assert socket.stat().st_mode & 0o777 == 0o600
        assert socket.parent.stat().st_mode & 0o777 == 0o700
        assert bridge.thread_config == {"features.bello_native_selection": True}
        assert await exchange_over_socket(bridge) == {"text": "selected\n"}
        assert len(selector.calls) == 1
        assert bridge.metrics["changed"] == 1
        telemetry = (tmp_path / "state/native-distiller.jsonl").read_text()
        assert "Check test failure" not in telemetry
        assert "original test log" not in telemetry
    finally:
        await bridge.close()
    assert not socket.exists()
    assert not socket.parent.exists()
    assert not selector.closed  # RuntimeClient owns the shared run worker.


async def test_windows_transport_is_authenticated_loopback_and_cleans_up(tmp_path, monkeypatch):
    selector = Selector()
    state = tmp_path / "state"
    # Replace only this module's reference; changing global os.name breaks Path.
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    bridge = module.CodexDistillerBridge(selector, state)
    await bridge.start()
    env = bridge.environment
    host, port = env["BELLO_SELECTOR_TCP"].split(":")
    assert host == "127.0.0.1" and 0 < int(port) < 65536
    assert "BELLO_SELECTOR_SOCKET" not in env
    try:
        assert await tcp_exchange(bridge) == {"text": "selected\n"}
        assert len(selector.calls) == 1
        telemetry = (state / "native-distiller.jsonl").read_text()
        assert env["BELLO_SELECTOR_TOKEN"] not in telemetry
        assert "original test log" not in telemetry
        assert bridge._directory is None and bridge._socket_path is None
    finally:
        await bridge.close()
    assert bridge._token is None and bridge._server is None
    with pytest.raises(RuntimeError, match="not started"):
        _ = bridge.environment
    assert not selector.closed


def client_proof(token, role, client_nonce, server_nonce):
    # Independent protocol construction also pins the cross-language wire bytes.
    data = b"bello-selector-" + role.encode() + b"-v1\0" + client_nonce.encode() + server_nonce.encode()
    return hmac.new(bytes.fromhex(token), data, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("role,expected", [
    ("server", "c81e01ef7f2511bbf4cfa67aef685dd774dbeb78964db4eaf2b9d09d046770ae"),
    ("client", "a04597c07a1a70fe870b484a052d4c094d8654562933d58b6f17ff004e061002"),
])
def test_authentication_wire_vectors_match_native_patch(role, expected):
    assert module._authentication_proof(bytes(32), role, "11" * 32, "22" * 32) == expected


async def tcp_exchange(bridge, *, token=None, replay=None, capture=None):
    env = bridge.environment
    host, port = env["BELLO_SELECTOR_TCP"].split(":")
    token = token or env["BELLO_SELECTOR_TOKEN"]
    reader, writer = await asyncio.open_connection(host, int(port))
    nonce = "1" * 64 if replay is not None else secrets.token_hex(32)
    try:
        writer.write(json.dumps({"version": 1, "nonce": nonce}).encode() + b"\n")
        await writer.drain()
        challenge = json.loads(await reader.readline())
        server_proof = client_proof(env["BELLO_SELECTOR_TOKEN"], "server", nonce, challenge["nonce"])
        assert hmac.compare_digest(challenge["proof"], server_proof)
        auth = replay or client_proof(token, "client", nonce, challenge["nonce"])
        if capture is not None:
            capture.append(auth)
        writer.write(json.dumps({"auth": auth, "command": "pytest -q", "focus": "Check failure",
                                 "log": "long original test log\n"}).encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
        return json.loads(line) if line else None
    finally:
        writer.close()
        await writer.wait_closed()


async def test_tcp_wrong_credentials_and_replayed_proof_never_run_selector(tmp_path):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path, transport="tcp")
    await bridge.start()
    try:
        capture = []
        assert await tcp_exchange(bridge, token="0" * 64, capture=capture) is None
        assert await tcp_exchange(bridge, replay=capture[0]) is None
        assert not selector.calls
        assert bridge.metrics["authentication_error"] == 2
    finally:
        await bridge.close()


async def test_tcp_successful_proof_cannot_be_reused_on_new_connection(tmp_path):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path, transport="tcp")
    await bridge.start()
    try:
        capture = []
        assert await tcp_exchange(bridge, replay="", capture=capture) == {"text": "selected\n"}
        assert await tcp_exchange(bridge, replay=capture[0]) is None
        assert len(selector.calls) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("greeting", [{}, {"version": 1, "nonce": "short"},
    {"version": True, "nonce": "a" * 64}, {"version": 1, "nonce": "A" * 64},
    {"version": 1, "nonce": "a" * 64, "padding": "x" * 1024}])
async def test_tcp_invalid_greetings_do_not_return_output(tmp_path, greeting):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path, transport="tcp")
    await bridge.start()
    try:
        host, port = bridge.environment["BELLO_SELECTOR_TCP"].split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(json.dumps(greeting).encode() + b"\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        assert not selector.calls
    finally:
        await bridge.close()


@pytest.mark.parametrize("command", [
    "cat TASK.md", "./reference --help", "cat README.md", "cat SPEC.json",
    "python -c 'print(open(\"CLAUDE.md\").read())'",
    "for arg in '--help' '-h'; do ./reference $arg; done",
])
async def test_native_critical_exclusions_skip_selector(tmp_path, command):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path)
    try:
        assert await exchange(bridge, command=command, log="native unchanged") == {"text": "native unchanged"}
        assert not selector.calls
        assert bridge.metrics["protected"] == 1
    finally:
        await bridge.close()


async def test_registered_task_scopes_survive_resume_revision(tmp_path):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path)
    bridge.register_scope(tmp_path / "first", Path("ticket.txt"))
    bridge.register_scope(tmp_path / "revision", Path("ticket.txt"))
    try:
        for root in (tmp_path / "first", tmp_path / "revision"):
            command = f"cat {shlex.quote((root / 'ticket.txt').as_posix())}"
            assert await exchange(bridge, command=command, log="task") == {"text": "task"}
        assert not selector.calls
        await exchange(bridge, command=f"cat {shlex.quote((tmp_path / 'other/ticket.txt').as_posix())}")
        assert len(selector.calls) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("value,focus,outcome", [
    ("much longer than original output", "Check", "unchanged"),
    (RuntimeError("unavailable"), "Check", "error"),
    ("selected", "", "missing_focus_or_command"),
    ("selected", "x" * 121, "missing_focus_or_command"),
])
async def test_fail_open_counted_never_changes_packet_shape(tmp_path, value, focus, outcome):
    bridge = module.CodexDistillerBridge(Selector(value), tmp_path)
    try:
        assert await exchange(bridge, focus=focus, log="original") == {"text": "original"}
        assert bridge.metrics[outcome] == 1
        assert bridge.metrics["changed"] == 0
    finally:
        await bridge.close()


async def test_close_cancels_active_inference(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Hanging(Selector):
        async def distill(self, *_args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    bridge = module.CodexDistillerBridge(Hanging(), tmp_path)
    client = asyncio.create_task(exchange(bridge))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(bridge.close(), 2)
    assert cancelled.is_set()
    await asyncio.gather(client, return_exceptions=True)


async def test_stock_binary_rejected_without_launch(monkeypatch, native_binary):
    binary = native_binary
    binary.write_bytes(b"unverified binary")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Must reject before process/model launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="verified selection-enabled"):
        await module.validate_native_selection([str(binary), "app-server"])


async def test_manifest_hash_deadline_and_real_feature_probe(monkeypatch, tmp_path, native_binary):
    binary = native_binary
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest = tmp_path / "capability.json"
    capability = {"binary_sha256": digest, "protocol": 1, "feature": "bello_native_selection",
                  "transport_timeout_seconds": 315, "transports": ["tcp-hmac-v1"]}
    manifest.write_text(json.dumps(capability))
    invocations = []

    class Probe:
        returncode = 0

        async def communicate(self):
            return b"bello_native_selection\tunder development\tfalse\n", b""

    async def spawn(*args, **kwargs):
        invocations.append(args)
        return Probe()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    verified = await module.validate_native_selection([str(binary), "app-server"], manifest)
    assert verified["binary_sha256"] == digest
    assert invocations == [(str(binary), "features", "list")]
    capability["transport_timeout_seconds"] = 135
    manifest.write_text(json.dumps(capability))
    with pytest.raises(RuntimeError, match="315-second"):
        await module.validate_native_selection([str(binary)], manifest)
    assert len(invocations) == 1
    capability["binary_sha256"] = "0" * 64
    manifest.write_text(json.dumps(capability))
    with pytest.raises(RuntimeError, match="does not match"):
        await module.validate_native_selection([str(binary)], manifest)


@pytest.mark.parametrize("payload", [None, b"not json", b"\xff", b" " * (64 * 1024 + 1)],
                         ids=["missing", "invalid-json", "invalid-utf8", "oversized"])
async def test_bad_manifest_is_actionable_and_never_launches(monkeypatch, tmp_path, native_binary, payload):
    binary = native_binary
    manifest = tmp_path / "capability.json"
    if payload is not None:
        manifest.write_bytes(payload)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Bad manifest must fail before probe or model calls")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="BELLO_CODEX_SELECTION_MANIFEST"):
        await module.validate_native_selection([str(binary)], manifest)


@pytest.mark.parametrize("feature,protocol,timeout", [
    ("bello_native_selection", True, 315),
    ("bello_native_selection", 1, True),
    ("bello_native_selection", 1, float("inf")),
    ("bello_native_selection", 2, 315),
    ("wrong_feature", 1, 315),
])
async def test_manifest_cannot_weaken_selection_requirements(monkeypatch, tmp_path, native_binary, feature, protocol, timeout):
    binary = native_binary
    manifest = tmp_path / "capability.json"
    manifest.write_text(json.dumps({"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "feature": feature, "protocol": protocol, "transport_timeout_seconds": timeout}))
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid capability must fail before process launch")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="verified selection-enabled"):
        await module.validate_native_selection([str(binary)], manifest)


async def test_manifest_does_not_replace_actual_feature_probe(monkeypatch, tmp_path, native_binary):
    binary = native_binary
    manifest = tmp_path / "capability.json"
    manifest.write_text(json.dumps({"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315,
        "transports": ["tcp-hmac-v1"]}))
    class Probe:
        returncode = 0
        async def communicate(self):
            return b"shell_tool stable true\n", b""
    async def spawn(*args, **kwargs):
        return Probe()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="does not advertise"):
        await module.validate_native_selection([str(binary)], manifest)


@pytest.mark.parametrize("transports", [None, [], ["unix"], "tcp-hmac-v1"])
async def test_windows_rejects_old_build_before_launch(tmp_path, monkeypatch, native_binary, transports):
    manifest = tmp_path / "capability.json"
    manifest.write_text(json.dumps({"binary_sha256": hashlib.sha256(native_binary.read_bytes()).hexdigest(),
        "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315,
        "transports": transports}))
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Old Windows build must be rejected before any launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="tcp-hmac-v1"):
        await module.validate_native_selection([str(native_binary)], manifest)
