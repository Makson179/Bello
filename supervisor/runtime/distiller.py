"""Optional, fail-open local log selection without model imports in the host."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

from supervisor.runtime.distiller_bundle import validate_bundle


logger = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_INPUT_BYTES = 16 * 1024 * 1024
SMALL_OUTPUT_MAX_BYTES = 200


def require_dependencies() -> None:
    """Check optional package presence without importing or loading the model."""
    missing = [name for name in ("torch", "transformers", "safetensors", "huggingface_hub")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            "Log distiller dependencies are missing: " + ", ".join(missing)
            + ". Install the optional extra in this Python environment with "
            + f"{sys.executable} -m pip install 'Bello[log-distiller]' "
            + "(for a source checkout: python -m pip install -e '.[log-distiller]')."
        )


class LogDistiller:
    """One lazily loaded CPU worker per run; requests share a serial deadline.

    ``model_path`` names a local bundle described by ``distiller_worker``. No
    subprocess or optional ML dependency is loaded until the first request.
    Errors preserve the original string. Call ``close`` when the run finishes.
    """

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path).expanduser().absolute()
        self._process: asyncio.subprocess.Process | None = None
        self._spawn: asyncio.Task | None = None
        self._request: asyncio.Task | None = None
        self._cleanup: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._unavailable = False
        self._sequence = 0

    async def distill(self, text: str, focus: str, command: str) -> str:
        """Keep outputs up to 200 UTF-8 bytes; otherwise select or fail open."""
        if (not text or self._closed or self._unavailable
                or not isinstance(focus, str) or not focus.strip()
                or not isinstance(command, str)):
            return text
        input_bytes = len(text.encode("utf-8"))
        # Bypass before acquiring the serial worker lock or loading the model.
        if input_bytes <= SMALL_OUTPUT_MAX_BYTES or input_bytes > MAX_INPUT_BYTES:
            return text
        acquired = False
        try:
            # Waiting for another call and model initialization are included.
            # There is no background inference queue after a call's deadline.
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                await self._lock.acquire()
                acquired = True
                if self._closed or self._unavailable:
                    return text
                self._sequence += 1
                request = {"id": self._sequence, "log": text, "focus": focus, "command": command}
                self._request = asyncio.create_task(self._exchange(request))
                result = await asyncio.shield(self._request)
                # Preserve native output when selection cannot make it smaller.
                return result if len(result.encode("utf-8")) < input_bytes else text
        except asyncio.CancelledError:
            if acquired:
                await self._finish_cleanup()
            if asyncio.current_task().cancelling():
                raise
            return text  # close() cancelled the owned worker request.
        except Exception as error:
            logger.warning("Log distiller retained original output (%s)", type(error).__name__)
            if acquired:
                await self._finish_cleanup()
            return text
        finally:
            if acquired:
                self._request = None
                self._lock.release()

    async def _exchange(self, request: dict) -> str:
        if self._process is None:
            environment = dict(os.environ)
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                               TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="")
            self._spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "supervisor.runtime.distiller_worker",
                "--model-path", str(self.model_path), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=environment, limit=MAX_INPUT_BYTES * 6 + 4096,
            ))
            # Cancellation during spawn must not orphan an unrecorded process.
            self._process = await asyncio.shield(self._spawn)
            self._spawn = None
            ready = await self._read_response()
            if ready != {"ready": True}:
                self._unavailable = True
                raise RuntimeError("model_bundle_unavailable")
        if self._closed:
            raise RuntimeError("distiller_closed")
        assert self._process.stdin is not None
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        self._process.stdin.write(payload)
        await self._process.stdin.drain()
        response = await self._read_response()
        if (response.get("id") != request["id"] or response.get("ok") is not True
                or not isinstance(response.get("text"), str)):
            raise ValueError("invalid_distiller_response")
        return response["text"]

    async def _read_response(self) -> dict:
        assert self._process is not None and self._process.stdout is not None
        line = await self._process.stdout.readline()
        if not line or not line.endswith(b"\n"):
            raise RuntimeError("distiller_worker_closed")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise ValueError("invalid_distiller_response")
        return result

    async def _stop_worker(self) -> None:
        request = self._request
        if request is not None and not request.done():
            request.cancel()
        if self._spawn is not None:
            try:
                self._process = await self._spawn
            except Exception:
                pass
            self._spawn = None
        process = self._process
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Drain stdout concurrently: Process.wait alone can wait forever on
            # a full asyncio pipe even after SIGKILL has terminated the worker.
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            try:
                await asyncio.wait_for(process.communicate(), timeout=10)
            except (TimeoutError, OSError):
                # Never start a second worker if the previous one was not reaped.
                self._unavailable = True
                logger.warning("Log distiller worker cleanup did not complete")
                return
            self._process = None
        elif request is not None:
            await asyncio.gather(request, return_exceptions=True)

    async def _finish_cleanup(self) -> None:
        if self._cleanup is None or self._cleanup.done():
            self._cleanup = asyncio.create_task(self._stop_worker())
        cancellation = None
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        self._cleanup.result()
        if cancellation is not None:
            raise cancellation

    async def close(self) -> None:
        """Stop and reap the owned worker, including an in-flight request."""
        self._closed = True
        await self._finish_cleanup()
