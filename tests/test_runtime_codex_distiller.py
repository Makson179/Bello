from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

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


async def exchange(bridge, *, command="pytest -q", focus="Check test failure", log="long original test log\n"):
    reader, writer = await asyncio.open_unix_connection(bridge.environment["BELLO_SELECTOR_SOCKET"])
    writer.write(json.dumps({"command": command, "focus": focus, "log": log}).encode() + b"\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


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
        assert await exchange(bridge) == {"text": "selected\n"}
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


@pytest.mark.parametrize("command", [
    "cat TASK.md", "./reference --help", "cat README.md", "cat SPEC.json",
    "python -c 'print(open(\"CLAUDE.md\").read())'",
    "for arg in '--help' '-h'; do ./reference $arg; done",
])
async def test_native_critical_exclusions_skip_selector(tmp_path, command):
    selector = Selector()
    bridge = module.CodexDistillerBridge(selector, tmp_path)
    await bridge.start()
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
    await bridge.start()
    try:
        for root in (tmp_path / "first", tmp_path / "revision"):
            assert await exchange(bridge, command=f"cat {root}/ticket.txt", log="task") == {"text": "task"}
        assert not selector.calls
        await exchange(bridge, command=f"cat {tmp_path}/other/ticket.txt")
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
    await bridge.start()
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
    await bridge.start()
    client = asyncio.create_task(exchange(bridge))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(bridge.close(), 2)
    assert cancelled.is_set()
    await asyncio.gather(client, return_exceptions=True)


async def test_stock_binary_rejected_without_launch(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"unverified binary")
    binary.chmod(0o700)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Must reject before process/model launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="verified selection-enabled"):
        await module.validate_native_selection([str(binary), "app-server"])


async def test_manifest_hash_deadline_and_real_feature_probe(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture native binary")
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest = tmp_path / "capability.json"
    capability = {"binary_sha256": digest, "protocol": 1, "feature": "bello_native_selection",
                  "transport_timeout_seconds": 315}
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


@pytest.mark.parametrize("payload", [None, b"not json", b"\xff", b" " * (64 * 1024 + 1)])
async def test_bad_manifest_is_actionable_and_never_launches(monkeypatch, tmp_path, payload):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture binary")
    binary.chmod(0o700)
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
async def test_manifest_cannot_weaken_selection_requirements(monkeypatch, tmp_path, feature, protocol, timeout):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture binary")
    binary.chmod(0o700)
    manifest = tmp_path / "capability.json"
    manifest.write_text(json.dumps({"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "feature": feature, "protocol": protocol, "transport_timeout_seconds": timeout}))
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid capability must fail before process launch")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="verified selection-enabled"):
        await module.validate_native_selection([str(binary)], manifest)


async def test_manifest_does_not_replace_actual_feature_probe(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture binary")
    binary.chmod(0o700)
    manifest = tmp_path / "capability.json"
    manifest.write_text(json.dumps({"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315}))
    class Probe:
        returncode = 0
        async def communicate(self):
            return b"shell_tool stable true\n", b""
    async def spawn(*args, **kwargs):
        return Probe()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="does not advertise"):
        await module.validate_native_selection([str(binary)], manifest)
