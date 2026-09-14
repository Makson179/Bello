"""Host-only bridge for a verified, opt-in native Codex output-selection build.

App-server notifications cannot replace model-visible native tool results. This
bridge uses the pinned native selection protocol: newline JSON {focus, command,
log} -> {text}. Native Codex retains execution, budgets, approvals and metadata.
Only coder threads explicitly enable the feature; the stock CLI is never replaced.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Sequence

from supervisor.runtime.distiller import LogDistiller, MAX_INPUT_BYTES, REQUEST_TIMEOUT_SECONDS
from supervisor.runtime.distiller_policy import preserve_tool_output


logger = logging.getLogger(__name__)
FOCUS_GUIDANCE = "Add a short focus to each command or poll call."
FEATURE_KEY = "features.bello_native_selection"
_FEATURE_NAME = "bello_native_selection"
_MAX_WIRE_BYTES = MAX_INPUT_BYTES * 6 + 4096
_MAX_MANIFEST_BYTES = 64 * 1024
# The original 0.149.0 build passed the model-visible-output fixture and the
# virtual-time regression accepting a response after 301 seconds.
_VERIFIED_BINARIES = {
    "24f94bd5a1181e6dbb9fdd195bff5b5639bd83f8c59d381fc9a7e3a3e154dacf": {
        "version": "0.149.0", "protocol": 1, "transport_timeout_seconds": 315,
        "feature": _FEATURE_NAME,
    },
    # 0.153.4 port: real-selector provider-boundary fixtures verified both
    # direct native output and Astra code-mode/write_stdin polling delivery.
    "49f183a9cbd91a7e87d0f44c27d1aa60f150359c44eab69084127888bd32dc6c": {
        "version": "0.153.4", "protocol": 1, "transport_timeout_seconds": 315,
        "feature": _FEATURE_NAME,
    },
}


async def validate_native_selection(
    command: Sequence[str], manifest_path: Path | None = None,
) -> dict:
    """Reject stock/unverified/short-deadline binaries before any paid turn.

    Known pinned binaries are accepted by SHA256. An explicitly supplied future
    build manifest must bind its SHA256, protocol, feature and >=315s deadline.
    The actual binary must also advertise the feature in its offline CLI list.
    """
    if not command or not isinstance(command[0], str):
        raise RuntimeError("Native log distiller requires an explicit Codex executable")
    executable = shutil.which(command[0])
    if executable is None:
        raise RuntimeError(f"Native Codex executable not found: {command[0]}")
    path = Path(executable).resolve()

    def digest() -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    sha256 = await asyncio.to_thread(digest)
    capability = _VERIFIED_BINARIES.get(sha256)
    if manifest_path is not None:
        try:
            with Path(manifest_path).open("rb") as stream:
                payload = stream.read(_MAX_MANIFEST_BYTES + 1)
            if len(payload) > _MAX_MANIFEST_BYTES:
                raise ValueError("manifest exceeds 64 KiB")
            manifest = json.loads(payload)
        except (OSError, ValueError, UnicodeError) as exc:
            raise RuntimeError(
                "Cannot read native Codex selection manifest; check "
                "BELLO_CODEX_SELECTION_MANIFEST points to a valid local JSON file"
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("binary_sha256") != sha256:
            raise RuntimeError("Native Codex selection manifest does not match the executable")
        capability = manifest
    if (not capability or type(capability.get("protocol")) is not int or capability.get("protocol") != 1
            or capability.get("feature") != _FEATURE_NAME
            or type(capability.get("transport_timeout_seconds")) not in (int, float)
            or not math.isfinite(capability["transport_timeout_seconds"])
            or capability["transport_timeout_seconds"] < REQUEST_TIMEOUT_SECONDS + 15):
        raise RuntimeError(
            "Native log distiller requires a verified selection-enabled Codex build "
            "with a 315-second transport deadline. Set BELLO_CODEX_BINARY to the patched "
            "executable and BELLO_CODEX_SELECTION_MANIFEST to its capability JSON for a "
            "custom build. Stock Codex and the older 135-second build cannot enable it. "
            "Alternatively disable log_distiller; see docs/native-codex-selection.md."
        )
    process = await asyncio.create_subprocess_exec(
        str(path), "features", "list", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode != 0 or not any(
        line.split() and line.split()[0] == _FEATURE_NAME for line in stdout.decode("utf-8", "replace").splitlines()
    ):
        raise RuntimeError("Selected Codex binary does not advertise native log selection")
    return {**capability, "binary_sha256": sha256, "binary_path": str(path)}


class CodexDistillerBridge:
    """One private Unix endpoint per run, borrowing the run's LogDistiller.

    Protocol v1 has no thread/cwd fields. Register each exact coder workspace/task
    before start/resume/revision; only their protected reads bypass selection.
    A missing focus and worker failures retain native output and are counted, not
    reported as successful compression. Receipts contain no log/focus contents.
    """

    def __init__(self, distiller: LogDistiller, state_dir: Path,
                 workspace: Path | None = None, task_path: Path | None = None):
        self.distiller = distiller
        self.state_dir = Path(state_dir)
        self.metrics: Counter = Counter()
        self._scopes: set[tuple[Path, Path | None]] = set()
        self._directory: tempfile.TemporaryDirectory | None = None
        self._socket_path: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task] = set()
        self._closed = False
        if workspace is not None:
            self.register_scope(workspace, task_path)

    def register_scope(self, workspace: Path, task_path: Path | None = None) -> None:
        root = Path(workspace).absolute()
        task = Path(task_path) if task_path is not None else None
        if task is not None and not task.is_absolute():
            task = root / task
        self._scopes.add((root, task))

    @property
    def environment(self) -> dict[str, str]:
        if self._socket_path is None or self._server is None:
            raise RuntimeError("Native distiller bridge has not started")
        return {"BELLO_SELECTOR_SOCKET": str(self._socket_path)}

    @property
    def thread_config(self) -> dict[str, bool]:
        return {FEATURE_KEY: True}

    async def start(self) -> None:
        if self._server is not None:
            return
        if self._closed:
            raise RuntimeError("Native distiller bridge is closed")
        if os.name == "nt":
            raise RuntimeError("This native selection build requires Unix sockets")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Short private path avoids the ~104-byte Unix socket path limit on macOS.
        self._directory = tempfile.TemporaryDirectory(prefix="bello-sel-")
        self._socket_path = Path(self._directory.name) / "selector.sock"
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=self._socket_path, limit=_MAX_WIRE_BYTES,
            )
            self._socket_path.chmod(0o600)
        except BaseException:
            self._directory.cleanup()
            self._directory = None
            self._socket_path = None
            raise

    def _record(self, outcome: str, before: int = 0, after: int = 0) -> None:
        self.metrics[outcome] += 1
        self.metrics["input_bytes"] += before
        self.metrics["output_bytes"] += after
        # Local telemetry only, never appended to the coder's output packet.
        try:
            with (self.state_dir / "native-distiller.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"outcome": outcome, "input_bytes": before, "output_bytes": after}) + "\n")
        except OSError:
            logger.warning("Could not write native distiller telemetry")

    async def _select(self, request: dict) -> str:
        original, focus, command = request["log"], request["focus"], request["command"]
        before = len(original.encode("utf-8"))
        if not original:
            outcome, selected = "empty", original
        elif before > MAX_INPUT_BYTES:
            outcome, selected = "oversized", original
        elif preserve_tool_output("exec_command", {"command": command}) or any(
            preserve_tool_output("exec_command", {"command": command}, task_path=task, workspace=root)
            for root, task in self._scopes
        ):
            outcome, selected = "protected", original
        elif not focus.strip() or len(focus) > 120 or not command.strip():
            outcome, selected = "missing_focus_or_command", original
        else:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    selected = await self.distiller.distill(original, focus, command)
                if not isinstance(selected, str):
                    raise ValueError("invalid selector text")
                if len(selected.encode("utf-8")) >= before:
                    selected = original
                outcome = "changed" if selected != original else "unchanged"
            except Exception as error:
                outcome, selected = "error", original
                logger.warning("Native distiller retained output (%s)", type(error).__name__)
        self._record(outcome, before, len(selected.encode("utf-8")))
        return selected

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.close()
            return
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            async with asyncio.timeout(5):
                line = await reader.readline()
            request = json.loads(line)
            if (not line.endswith(b"\n") or not isinstance(request, dict)
                    or not all(isinstance(request.get(key), str) for key in ("log", "focus", "command"))):
                raise ValueError("invalid native selection request")
            selected = await self._select(request)
            writer.write(json.dumps({"text": selected}, ensure_ascii=False).encode("utf-8") + b"\n")
            async with asyncio.timeout(5):
                await writer.drain()
        except (ValueError, UnicodeError, OSError, TimeoutError):
            self._record("protocol_error")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            finally:
                self._handlers.discard(task)

    async def close(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
        for task in self._handlers.copy():
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
            self._socket_path = None
