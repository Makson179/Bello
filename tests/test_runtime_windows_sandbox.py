from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import struct
import sys
from typing import Any

import pytest

from supervisor.runtime import windows_sandbox


_FAKE_HELPER = r"""
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time

if os.name == "nt":
    import msvcrt

    # Match the native helper's byte transport, not the CRT text translation.
    # Frame lengths may contain CR, LF, or Ctrl-Z; output must stay byte-exact.
    for descriptor in (0, 1, 2):
        msvcrt.setmode(descriptor, os.O_BINARY)


run_kind, recovery_kind, log_name = sys.argv[1:4]
log_path = Path(log_name)


def log(event, **values):
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **values}, ensure_ascii=False) + "\n")


def read_exact(size):
    value = b""
    while len(value) < size:
        chunk = os.read(0, size - len(value))
        if not chunk:
            raise RuntimeError("unexpected EOF in request frame")
        value += chunk
    return value


def terminal(value):
    os.write(2, json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")


header = read_exact(4)
announced = struct.unpack("<I", header)[0]
body = read_exact(announced)
request = json.loads(body.decode("utf-8"))
operation = request.get("operation")
log(
    "request",
    operation=operation,
    announcedLength=announced,
    bodyLength=len(body),
    request=request,
)

if operation == "recover":
    log("recover_started")
    trailing = os.read(0, 1)
    log("recover_eof", observed=trailing == b"")
    if recovery_kind == "slow":
        time.sleep(0.25)
    elif recovery_kind == "hang":
        time.sleep(60)
    if recovery_kind == "error":
        terminal(
            {
                "protocolVersion": 1,
                "kind": "error",
                "code": "RECOVERY_DENIED",
                "message": "recovery failed",
            }
        )
        raise SystemExit(9)
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 0})
    log("recover_done")
    raise SystemExit(0)

if operation != "run":
    terminal(
        {
            "protocolVersion": 1,
            "kind": "error",
            "code": "BAD_OPERATION",
            "message": "unexpected operation",
        }
    )
    raise SystemExit(2)

log("run_started")

if run_kind == "requires_open_stdin":
    probe = []

    def read_trailing_byte():
        probe.append(os.read(0, 1))

    thread = threading.Thread(target=read_trailing_byte, daemon=True)
    thread.start()
    time.sleep(0.05)
    if probe:
        terminal(
            {
                "protocolVersion": 1,
                "kind": "error",
                "code": "EARLY_EOF",
                "message": "stdin closed before command completion",
            }
        )
        raise SystemExit(3)
    log("run_stdin_held_open")
    os.write(1, b"ok")
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 23})
    raise SystemExit(0)

if run_kind == "private_control":
    os.write(1, b'{"protocolVersion":1,"kind":"exit","exitCode":999}\n')
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 7})
    raise SystemExit(0)

if run_kind == "split_utf8":
    os.write(1, b"A\xf0\x9f")
    time.sleep(0.03)
    os.write(1, b"\x98\x80BCDE")
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 0})
    raise SystemExit(0)

if run_kind == "error_zero" or run_kind == "error_nonzero":
    terminal(
        {
            "protocolVersion": 1,
            "kind": "error",
            "code": "E_DENIED",
            "message": "blocked",
        }
    )
    raise SystemExit(0 if run_kind == "error_zero" else 17)

if run_kind == "exit_nonzero":
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 4})
    raise SystemExit(9)

if run_kind == "missing_control":
    os.write(1, b"output-before-eof")
    raise SystemExit(0)

if run_kind in {"wait_eof", "callback_error"}:
    if run_kind == "callback_error":
        os.write(1, b"callback-data")
    trailing = os.read(0, 1)
    log("run_eof", observed=trailing == b"")
    terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 0})
    raise SystemExit(0)

terminal({"protocolVersion": 1, "kind": "exit", "exitCode": 0})
raise SystemExit(0)
"""


@dataclass
class FakeHelperHarness:
    root: Path
    cwd: Path
    script: Path
    log: Path
    run_kind: str = "requires_open_stdin"
    recovery_kind: str = "ok"
    spawn_environments: list[dict[str, str]] = field(default_factory=list)

    def events(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    async def wait_for(self, event: str, *, timeout: float = 3) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            match = next((item for item in self.events() if item["event"] == event), None)
            if match is not None:
                return match
            await asyncio.sleep(0.01)
        raise AssertionError(f"fake helper did not report {event!r}; events={self.events()!r}")


@pytest.fixture
def fake_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHelperHarness:
    root = tmp_path / "workspace"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    script = tmp_path / "fake_windows_helper.py"
    script.write_text(_FAKE_HELPER, encoding="utf-8")
    harness = FakeHelperHarness(root=root, cwd=cwd, script=script, log=tmp_path / "helper.jsonl")

    def helper_path(requested_root: Path, mode: str) -> Path:
        assert requested_root == root
        assert mode in {"read-only", "workspace-write"}
        return tmp_path / "bello-windows-sandbox.exe"

    async def spawn(
        helper: Path,
        environment: dict[str, str],
        *,
        cwd: Path,
    ) -> asyncio.subprocess.Process:
        assert helper == tmp_path / "bello-windows-sandbox.exe"
        harness.spawn_environments.append(dict(environment))
        return await asyncio.create_subprocess_exec(
            sys.executable,
            os.fspath(script),
            harness.run_kind,
            harness.recovery_kind,
            os.fspath(harness.log),
            cwd=os.fspath(cwd),
            env={},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(windows_sandbox, "_helper_path", helper_path)
    monkeypatch.setattr(windows_sandbox, "_spawn", spawn)
    return harness


def _run_kwargs(harness: FakeHelperHarness, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "command": "python -m pytest",
        "cwd": harness.cwd,
        "root": harness.root,
        "mode": "workspace-write",
        "readable_roots": [harness.root],
        "private_paths": [harness.root.parent / "controller-private"],
        "network_access": False,
        "timeout": 2,
        "on_output": None,
        "cancel_event": None,
        "max_output_chars": 4096,
        "truncated_text": "<truncated>",
    }
    values.update(overrides)
    return values


def test_request_uses_little_endian_length_framing_and_strict_utf8_json() -> None:
    request = {"operation": "run", "protocolVersion": 1, "command": "echo Привет"}

    frame = windows_sandbox._encode_request(request)

    announced = struct.unpack("<I", frame[:4])[0]
    assert announced == len(frame) - 4
    assert json.loads(frame[4:].decode("utf-8")) == request
    assert b'": ' not in frame[4:]
    assert b', "' not in frame[4:]


def test_request_rejects_non_json_numbers_and_oversized_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match="not valid JSON"):
        windows_sandbox._encode_request({"value": float("nan")})

    monkeypatch.setattr(windows_sandbox, "MAX_REQUEST_BYTES", 8)
    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match="exceeds 8 bytes"):
        windows_sandbox._encode_request({"too": "large"})


def test_terminal_parser_accepts_exact_exit_and_error_records() -> None:
    exit_record = windows_sandbox._parse_terminal(
        b'{"protocolVersion":1,"kind":"exit","exitCode":-7}\n'
    )
    error_record = windows_sandbox._parse_terminal(
        b'{"protocolVersion":1,"kind":"error","code":"DENIED","message":"blocked"}\n'
    )

    assert exit_record.exit_code == -7
    assert error_record.code == "DENIED"
    assert error_record.message == "blocked"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "missing or oversized"),
        (b'{"protocolVersion":1,"kind":"exit","exitCode":0}', "exactly one"),
        (b'{}\n{}\n', "exactly one"),
        (b"\xff\n", "not valid UTF-8 JSON"),
        (b'{"protocolVersion":2,"kind":"exit","exitCode":0}\n', "version mismatch"),
        (b'{"protocolVersion":1,"kind":"exit","exitCode":true}\n', "signed 32-bit"),
        (b'{"protocolVersion":1,"kind":"exit","exitCode":2147483648}\n', "signed 32-bit"),
        (b'{"protocolVersion":1,"kind":"exit","exitCode":0,"extra":1}\n', "unexpected fields"),
        (b'{"protocolVersion":1,"kind":"error","code":"","message":"x"}\n', "malformed"),
        (b'{"protocolVersion":1,"kind":"other"}\n', "unknown kind"),
    ],
)
def test_terminal_parser_rejects_noncanonical_control_records(raw: bytes, message: str) -> None:
    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match=message):
        windows_sandbox._parse_terminal(raw)


async def test_read_control_rejects_data_over_the_bounded_control_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_sandbox, "MAX_CONTROL_BYTES", 16)
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 17)
    stream.feed_eof()

    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match="oversized"):
        await windows_sandbox._read_control(stream)


async def test_bridge_sends_exact_run_contract_with_empty_environment_and_open_stdin(
    fake_helper: FakeHelperHarness,
) -> None:
    outcome = await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))

    assert outcome == windows_sandbox.WindowsSandboxOutcome("ok", 23)
    request_event = next(item for item in fake_helper.events() if item["event"] == "request")
    assert request_event["announcedLength"] == request_event["bodyLength"]
    assert request_event["request"] == {
        "operation": "run",
        "protocolVersion": windows_sandbox.PROTOCOL_VERSION,
        "command": "python -m pytest",
        "cwd": os.fspath(fake_helper.cwd),
        "root": os.fspath(fake_helper.root),
        "mode": "workspace-write",
        "readableRoots": [os.fspath(fake_helper.root)],
        "privatePaths": [os.fspath(fake_helper.root.parent / "controller-private")],
        "networkAccess": False,
    }
    assert any(item["event"] == "run_stdin_held_open" for item in fake_helper.events())
    assert fake_helper.spawn_environments == [{}]


async def test_terminal_json_on_untrusted_stdout_is_output_not_control(
    fake_helper: FakeHelperHarness,
) -> None:
    fake_helper.run_kind = "private_control"

    outcome = await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))

    assert outcome.output == '{"protocolVersion":1,"kind":"exit","exitCode":999}\n'
    assert outcome.exit_code == 7


@pytest.mark.parametrize("length_low_byte", [0x0A, 0x0D, 0x1A])
async def test_fake_helper_preserves_binary_frame_lengths_and_output(
    fake_helper: FakeHelperHarness,
    length_low_byte: int,
) -> None:
    fake_helper.run_kind = "private_control"
    kwargs = _run_kwargs(fake_helper)
    request = windows_sandbox._run_request(
        **{
            name: kwargs[name]
            for name in (
                "command", "cwd", "root", "mode", "readable_roots", "private_paths",
                "network_access",
            )
        }
    )
    body_length = len(windows_sandbox._encode_request(request)) - 4
    padding = (length_low_byte - body_length) % 256
    kwargs["command"] += "x" * padding

    outcome = await windows_sandbox.run_restricted(**kwargs)

    event = next(item for item in fake_helper.events() if item["event"] == "request")
    assert event["announcedLength"] % 256 == length_low_byte
    assert event["bodyLength"] == event["announcedLength"]
    assert event["request"]["command"] == kwargs["command"]
    assert outcome.exit_code == 7
    assert outcome.output == '{"protocolVersion":1,"kind":"exit","exitCode":999}\n'


async def test_split_utf8_output_is_decoded_incrementally_and_truncated_once(
    fake_helper: FakeHelperHarness,
) -> None:
    fake_helper.run_kind = "split_utf8"
    streamed: list[str] = []

    async def on_output(text: str) -> None:
        streamed.append(text)

    outcome = await windows_sandbox.run_restricted(
        **_run_kwargs(
            fake_helper,
            max_output_chars=3,
            truncated_text="<cut>",
            on_output=on_output,
        )
    )

    assert outcome.output == "A😀B<cut>"
    assert "".join(streamed) == outcome.output
    assert streamed.count("<cut>") == 1


@pytest.mark.parametrize(
    ("run_kind", "message"),
    [
        ("error_zero", "error record with exit status 0"),
        ("error_nonzero", "E_DENIED: blocked"),
        ("exit_nonzero", "helper exited 9 after an exit record"),
    ],
)
async def test_helper_error_records_and_process_exit_status_cannot_contradict(
    fake_helper: FakeHelperHarness,
    run_kind: str,
    message: str,
) -> None:
    fake_helper.run_kind = run_kind

    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match=message):
        await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))

    assert any(item["event"] == "recover_done" for item in fake_helper.events())
    assert fake_helper.spawn_environments == [{}, {}]


@pytest.mark.parametrize("abort_kind", ["timeout", "cancel_event"])
async def test_timeout_and_cancel_event_signal_eof_then_complete_recovery(
    fake_helper: FakeHelperHarness,
    abort_kind: str,
) -> None:
    fake_helper.run_kind = "wait_eof"
    cancel_event = asyncio.Event()
    timeout = 0.08 if abort_kind == "timeout" else 2
    task = asyncio.create_task(
        windows_sandbox.run_restricted(
            **_run_kwargs(fake_helper, timeout=timeout, cancel_event=cancel_event)
        )
    )
    await fake_helper.wait_for("run_started")
    if abort_kind == "cancel_event":
        cancel_event.set()

    outcome = await asyncio.wait_for(task, 3)

    assert outcome.exit_code == (124 if abort_kind == "timeout" else 130)
    assert outcome.timed_out is (abort_kind == "timeout")
    assert outcome.cancelled is (abort_kind == "cancel_event")
    assert next(item for item in fake_helper.events() if item["event"] == "run_eof")["observed"]
    assert next(item for item in fake_helper.events() if item["event"] == "recover_eof")["observed"]
    assert any(item["event"] == "recover_done" for item in fake_helper.events())


async def test_repeated_task_cancellation_cannot_interrupt_recovery(
    fake_helper: FakeHelperHarness,
) -> None:
    fake_helper.run_kind = "wait_eof"
    fake_helper.recovery_kind = "slow"
    task = asyncio.create_task(windows_sandbox.run_restricted(**_run_kwargs(fake_helper)))
    await fake_helper.wait_for("run_started")

    task.cancel()
    await fake_helper.wait_for("recover_started")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert any(item["event"] == "recover_done" for item in fake_helper.events())


async def test_output_callback_error_is_preserved_after_successful_recovery(
    fake_helper: FakeHelperHarness,
) -> None:
    class CallbackFailure(RuntimeError):
        pass

    fake_helper.run_kind = "callback_error"

    async def broken_callback(_text: str) -> None:
        raise CallbackFailure("consumer stopped")

    with pytest.raises(CallbackFailure, match="consumer stopped"):
        await windows_sandbox.run_restricted(
            **_run_kwargs(fake_helper, on_output=broken_callback)
        )

    assert any(item["event"] == "run_eof" for item in fake_helper.events())
    assert any(item["event"] == "recover_done" for item in fake_helper.events())


async def test_missing_control_record_recovers_before_reporting_protocol_failure(
    fake_helper: FakeHelperHarness,
) -> None:
    fake_helper.run_kind = "missing_control"

    with pytest.raises(windows_sandbox.WindowsSandboxBackendError, match="missing or oversized"):
        await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))

    assert any(item["event"] == "recover_done" for item in fake_helper.events())


async def test_recovery_error_fails_closed_instead_of_returning_original_result(
    fake_helper: FakeHelperHarness,
) -> None:
    fake_helper.run_kind = "missing_control"
    fake_helper.recovery_kind = "error"

    with pytest.raises(
        windows_sandbox.WindowsSandboxBackendError,
        match="Windows sandbox cleanup failed: Windows sandbox recovery failed: RECOVERY_DENIED",
    ):
        await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))


async def test_recovery_timeout_fails_closed_and_kills_the_recovery_helper(
    fake_helper: FakeHelperHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_helper.run_kind = "missing_control"
    fake_helper.recovery_kind = "hang"
    monkeypatch.setattr(windows_sandbox, "_RECOVERY_TIMEOUT_SECONDS", 0.08)

    with pytest.raises(
        windows_sandbox.WindowsSandboxBackendError,
        match="Windows sandbox cleanup failed: Windows sandbox recovery timed out",
    ):
        await windows_sandbox.run_restricted(**_run_kwargs(fake_helper))


async def test_preset_cancellation_neither_resolves_paths_nor_spawns_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()

    def forbidden_helper_path(*_args: Any, **_kwargs: Any) -> Path:
        raise AssertionError("pre-cancelled runs must not resolve the helper")

    async def forbidden_spawn(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        raise AssertionError("pre-cancelled runs must not spawn the helper")

    monkeypatch.setattr(windows_sandbox, "_helper_path", forbidden_helper_path)
    monkeypatch.setattr(windows_sandbox, "_spawn", forbidden_spawn)

    outcome = await windows_sandbox.run_restricted(
        command="echo never",
        cwd=tmp_path / "missing-cwd",
        root=tmp_path / "missing-root",
        mode="read-only",
        readable_roots=[tmp_path / "missing-readable"],
        private_paths=[tmp_path / "missing-private"],
        network_access=False,
        timeout=1,
        on_output=None,
        cancel_event=cancel_event,
        max_output_chars=10,
        truncated_text="<cut>",
    )

    assert outcome == windows_sandbox.WindowsSandboxOutcome("", 130, cancelled=True)
