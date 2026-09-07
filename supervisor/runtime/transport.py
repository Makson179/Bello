"""Bidirectional JSONL transport for the pinned, trusted Pi worker.

Tool callbacks execute independently of the reader so approving one tool can
call a different model without deadlocking the stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from supervisor.appserver import AppServerError, AppServerProtocolError, AppServerTimeoutError
from supervisor.runtime.cleanup import finish_cleanup


class WorkerTransport:
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        *,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        on_error: Callable[[BaseException], Awaitable[None]],
        env: dict[str, str] | None = None,
    ):
        self.command, self.cwd, self.emit, self.tool, self.on_error = command, cwd, emit, tool, on_error
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._calls: dict[int | str, asyncio.Task[None]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._sequence = 0
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._notifying_error = False
        self._stop_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        self._closing = False
        self._stop_task = None
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.cwd, env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024, **({"start_new_session": True} if os.name != "nt" else {}),
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if self.process is None or self._closing:
            raise AppServerError("Pi worker is not started")
        self._sequence += 1
        request_id = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await asyncio.wait_for(self._send({"id": request_id, "method": method, "params": params or {}}), timeout)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise AppServerTimeoutError(f"Pi worker {method} timed out; an uncertain action is not retried") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _send(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._closing or not self.process or not self.process.stdin or self.process.returncode is not None:
                raise AppServerError("Pi worker stream is closed")
            self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await self.process.stdin.drain()

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise AppServerProtocolError("Pi worker message must be an object")
                request_id = message.get("id")
                if "method" not in message:
                    future = self._pending.get(request_id)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(AppServerError(f"Pi worker error: {message['error']}"))
                        elif isinstance(message.get("result"), dict):
                            future.set_result(message["result"])
                        else:
                            raise AppServerProtocolError("Pi worker response must contain an object result")
                elif request_id is not None:
                    if message["method"] != "bello/tool":
                        await self._send({"id": request_id, "error": {"message": "unsupported host request"}})
                    elif request_id in self._calls:
                        raise AppServerProtocolError("duplicate active host request id")
                    else:
                        self._calls[request_id] = asyncio.create_task(self._run_tool(message))
                elif message["method"] == "bello/tool/cancel":
                    call = self._calls.get(message.get("params", {}).get("requestId"))
                    if call:
                        call.cancel()
                else:
                    await self.emit(message)
            if not self._closing:
                raise AppServerError("Pi worker stream closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            notify = not self._closing
            self._closing = True
            self._fail_pending(exc)
            calls = list(self._calls.values())
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            # A broken reader must not leave an unobserved model loop running
            # while the controller decides how to recover.
            try:
                await self._terminate_process()
            except Exception as cleanup_error:
                exc.add_note(f"Pi worker termination failed: {type(cleanup_error).__name__}")
            finally:
                if notify:
                    self._notifying_error = True
                    try:
                        await self.on_error(exc)
                    finally:
                        self._notifying_error = False

    async def _run_tool(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        try:
            result = await self.tool(message.get("params", {}))
            await self._send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            if not self._closing:
                try:
                    await self._send({"id": request_id, "error": {"message": "tool cancelled"}})
                except (OSError, AppServerError):
                    pass
            raise
        except Exception as exc:
            if not self._closing:
                try:
                    await self._send({"id": request_id, "error": {"message": str(exc)}})
                except (OSError, AppServerError):
                    # Reader/transport failure owns the recovery notification.
                    # This tool must not leak an unobserved task exception.
                    pass
        finally:
            self._calls.pop(request_id, None)

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        # Third-party stderr may contain URLs with authentication parameters.
        # Do not forward it into model context, logs or user-visible exceptions.
        while await self.process.stderr.read(65536):
            pass

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _terminate_process(self) -> None:
        process = self.process
        if process and process.returncode is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError):
                if process.returncode is None:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(asyncio.current_task()))
        await finish_cleanup(self._stop_task)

    async def _stop(self, caller: asyncio.Task | None) -> None:
        self._closing = True
        calls = list(self._calls.values())
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        await self._terminate_process()
        # A failure callback may itself ask the owning RuntimeClient to stop.
        # The reader has already terminated the worker and will return after
        # that callback; cancelling/awaiting it here would create a cycle.
        readers = [task for task in (self._reader, self._stderr) if task and task is not caller
                   and not (task is self._reader and self._notifying_error)]
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        self._fail_pending(AppServerError("Pi worker stopped"))
        self.process = None
