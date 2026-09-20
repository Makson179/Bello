from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sys

import pytest

from supervisor.runtime.command_sessions import (
    CommandCompletion,
    CommandSessionError,
    CommandSessionManager,
)
from supervisor.runtime.sandbox import SandboxPolicy, SandboxResult, SandboxRunner


THREAD = "thread-1"
TURN = "turn-1"
CALL = "call-1"


class QueueRunner:
    def __init__(self):
        self.actions: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self.started = asyncio.Event()
        self.calls = 0
        self.output = ""

    def emit(self, text: str) -> None:
        self.actions.put_nowait(("output", text))

    def finish(self, exit_code: int = 0) -> None:
        self.actions.put_nowait(("finish", exit_code))

    def fail(self, error: BaseException) -> None:
        self.actions.put_nowait(("error", error))

    async def run(self, _command, _cwd, _timeout, on_output=None, *, cancel_event=None):
        self.calls += 1
        self.started.set()
        assert cancel_event is not None
        while True:
            action_task = asyncio.create_task(self.actions.get())
            cancel_task = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                {action_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if cancel_task in done and cancel_task.result():
                return SandboxResult(self.output, 130, 0.01, cancelled=True)
            kind, value = action_task.result()
            if kind == "output":
                assert isinstance(value, str)
                self.output += value
                if on_output is not None:
                    await on_output(value)
            elif kind == "finish":
                assert isinstance(value, int)
                return SandboxResult(self.output, value, 0.02)
            else:
                assert isinstance(value, BaseException)
                raise value


class ImmediateRunner:
    def __init__(self, result: SandboxResult | None = None, error: Exception | None = None):
        self.result = result or SandboxResult("done\n", 0, 0.01)
        self.error = error
        self.calls = 0

    async def run(self, _command, _cwd, _timeout, on_output=None, *, cancel_event=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if on_output is not None and self.result.output:
            await on_output(self.result.output)
        return self.result


async def _start(
    manager: CommandSessionManager,
    runner,
    tmp_path: Path,
    *,
    thread_id: str = THREAD,
    turn_id: str = TURN,
    call_id: str = CALL,
    command: str = "run tests",
    yield_time_ms: int = 0,
    on_output=None,
    on_finished=None,
):
    return await manager.start(
        thread_id=thread_id,
        turn_id=turn_id,
        call_id=call_id,
        runner=runner,
        command=command,
        cwd=tmp_path,
        timeout=60,
        yield_time_ms=yield_time_ms,
        on_output=on_output,
        on_finished=on_finished,
    )


@pytest.mark.asyncio
async def test_fast_command_returns_terminal_and_callbacks_once(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    streamed: list[str] = []
    completions: list[CommandCompletion] = []

    async def on_output(chunk: str) -> None:
        streamed.append(chunk)

    async def on_finished(completion: CommandCompletion) -> None:
        completions.append(completion)

    result = await _start(
        manager,
        ImmediateRunner(),
        tmp_path,
        yield_time_ms=1000,
        on_output=on_output,
        on_finished=on_finished,
    )

    assert result["status"] == "completed"
    assert result["output"] == "done\n"
    assert result["aggregatedOutput"] == "done\n"
    assert result["exitCode"] == 0
    assert streamed == ["done\n"]
    assert len(completions) == 1
    assert completions[0].result == SandboxResult("done\n", 0, 0.01)
    assert completions[0].error is None
    await manager.stop(thread_id=THREAD, turn_id=TURN, session_id=result["sessionId"])
    assert len(completions) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_yielded_command_streams_incrementally_then_completes(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    runner = QueueRunner()
    started = await _start(manager, runner, tmp_path)
    await runner.started.wait()

    assert started["status"] == "running"
    runner.emit("building\n")
    first = await manager.poll(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
        yield_time_ms=1000,
    )
    assert first["status"] == "running"
    assert first["output"] == "building\n"

    runner.emit("ready\n")
    runner.finish()
    terminal = await manager.poll(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
        yield_time_ms=1000,
    )
    assert terminal["status"] == "completed"
    assert terminal["output"] == "ready\n"
    assert terminal["aggregatedOutput"] == "building\nready\n"
    assert terminal["exitCode"] == 0
    assert runner.calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_same_call_never_starts_twice_or_changes_arguments(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    first_runner = QueueRunner()
    first = await _start(manager, first_runner, tmp_path)
    await first_runner.started.wait()
    second_runner = QueueRunner()

    replay = await _start(manager, second_runner, tmp_path)
    assert replay["sessionId"] == first["sessionId"]
    assert first_runner.calls == 1
    assert second_runner.calls == 0
    with pytest.raises(CommandSessionError, match="different arguments"):
        await _start(manager, second_runner, tmp_path, command="different command")
    assert second_runner.calls == 0
    await manager.close()


@pytest.mark.asyncio
async def test_stop_is_owned_idempotent_and_finishes_callback_once(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    runner = QueueRunner()
    completions: list[CommandCompletion] = []

    async def finished(value: CommandCompletion) -> None:
        completions.append(value)

    started = await _start(manager, runner, tmp_path, on_finished=finished)
    await runner.started.wait()
    with pytest.raises(CommandSessionError, match="not owned"):
        await manager.stop(
            thread_id="another-thread",
            turn_id=TURN,
            session_id=started["sessionId"],
        )

    stopped = await manager.stop(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
    )
    assert stopped["status"] == "cancelled"
    assert stopped["exitCode"] == 130
    assert stopped["cancelled"] is True
    again = await manager.stop(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
    )
    assert again["status"] == "cancelled"
    assert len(completions) == 1
    assert completions[0].status == "cancelled"
    assert completions[0].result is not None and completions[0].result.cancelled
    await manager.close()


@pytest.mark.asyncio
async def test_turn_cleanup_stops_only_owned_sessions(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    first_runner, second_runner = QueueRunner(), QueueRunner()
    first = await _start(manager, first_runner, tmp_path, call_id="first")
    second = await _start(
        manager,
        second_runner,
        tmp_path,
        turn_id="turn-2",
        call_id="second",
    )
    await asyncio.gather(first_runner.started.wait(), second_runner.started.wait())

    await manager.cancel_turn(THREAD, TURN)
    assert (
        await manager.poll(
            thread_id=THREAD, turn_id=TURN, session_id=first["sessionId"]
        )
    )["status"] == "cancelled"
    assert (
        await manager.poll(
            thread_id=THREAD, turn_id="turn-2", session_id=second["sessionId"]
        )
    )["status"] == "running"
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["turn", "thread", "close"])
async def test_cleanup_joins_terminal_session_until_finished_callback_returns(
    tmp_path: Path, cleanup: str
) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_finished = asyncio.Event()
    completions: list[CommandCompletion] = []

    async def on_finished(completion: CommandCompletion) -> None:
        completions.append(completion)
        callback_started.set()
        await release_callback.wait()
        callback_finished.set()

    started = await _start(
        manager,
        ImmediateRunner(),
        tmp_path,
        yield_time_ms=0,
        on_finished=on_finished,
    )
    await callback_started.wait()
    assert started["status"] == "completed"
    assert callback_finished.is_set() is False

    if cleanup == "turn":
        cleanup_task = asyncio.create_task(manager.cancel_turn(THREAD, TURN))
    elif cleanup == "thread":
        cleanup_task = asyncio.create_task(manager.cancel_thread(THREAD))
    else:
        cleanup_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    assert cleanup_task.done() is False

    release_callback.set()
    await cleanup_task
    assert callback_finished.is_set() is True
    assert len(completions) == 1
    if cleanup != "close":
        await manager.close()


@pytest.mark.asyncio
async def test_output_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    manager = CommandSessionManager(
        tmp_path / "state",
        max_output_chars=10,
        max_response_chars=5,
        max_active=1,
        max_active_per_turn=1,
        max_records=2,
    )
    runner = QueueRunner()
    started = await _start(manager, runner, tmp_path)
    runner.emit("abcdefghijkl")
    runner.finish()
    terminal = await manager.poll(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
        yield_time_ms=1000,
    )

    assert terminal["status"] == "completed"
    assert terminal["output"] == "hijkl"
    assert terminal["aggregatedOutput"] == "hijkl"
    assert terminal["outputTruncated"] is True
    await manager.close()


@pytest.mark.asyncio
async def test_recovery_marks_running_uncertain_and_never_replays(tmp_path: Path) -> None:
    state = tmp_path / "state"
    original = CommandSessionManager(state)
    runner = QueueRunner()
    started = await _start(original, runner, tmp_path)
    await runner.started.wait()

    recovered = CommandSessionManager(state)
    lost = await recovered.poll(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
    )
    assert lost["status"] == "lost"
    assert "uncertain" in lost["error"]

    replacement = QueueRunner()
    replay = await _start(recovered, replacement, tmp_path)
    assert replay["status"] == "lost"
    assert replacement.calls == 0
    await original.close()
    await recovered.close()


@pytest.mark.asyncio
async def test_runner_error_and_callback_error_are_terminal_without_retry(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    completions: list[CommandCompletion] = []

    async def callback(completion: CommandCompletion) -> None:
        completions.append(completion)
        raise RuntimeError("event sink closed")

    result = await _start(
        manager,
        ImmediateRunner(error=ValueError("runner broke")),
        tmp_path,
        yield_time_ms=1000,
        on_finished=callback,
    )
    assert result["status"] == "failed"
    assert "runner broke" in result["error"]
    assert "event sink closed" in result["callbackError"]
    assert len(completions) == 1
    assert isinstance(completions[0].error, ValueError)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="the fixture command uses the POSIX shell")
async def test_real_sandbox_runner_keeps_local_server_owned_until_stop(tmp_path: Path) -> None:
    manager = CommandSessionManager(tmp_path / "state")
    runner = SandboxRunner(SandboxPolicy(tmp_path, "danger-full-access"))
    command = shlex.join(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('server-ready', flush=True); time.sleep(60)",
        ]
    )
    started = await _start(manager, runner, tmp_path, command=command)
    assert started["status"] == "running"
    ready = await manager.poll(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
        yield_time_ms=2000,
    )
    assert ready["status"] == "running"
    assert "server-ready" in ready["output"]

    stopped = await manager.stop(
        thread_id=THREAD,
        turn_id=TURN,
        session_id=started["sessionId"],
    )
    assert stopped["status"] == "cancelled"
    assert stopped["exitCode"] == 130
    await manager.close()


def test_state_registry_rejects_symbolic_link(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symbolic links are unavailable")
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (state / "sessions.json").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(CommandSessionError, match="cannot be a link"):
        CommandSessionManager(state)
