#!/usr/bin/env python3
"""Offline native Codex output-selection proof at the next provider request.

Only synthetic files and a loopback Responses provider are used. Every Codex
process has a new empty HOME/CODEX_HOME, no inherited credentials, and proxies
that reject external requests. A deterministic selector double tests transport,
not model quality. The real CodexDistillerBridge supplies protection/scope logic.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RAW = ("NOISE_SENTINEL successful redundant diagnostic line\n" * 80
       + "KEEP_SENTINEL failure: expected 3, received 4\n")
SELECTED = "KEEP_SENTINEL failure: expected 3, received 4\n"
FOCUS = "Identify the failing assertion."
POLL_FOCUS = "Identify the failing assertion after completion."
CALL_ID = "selection-fixture-call"
LIMIT = 8 * 1024 * 1024


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class Case:
    name: str
    mode: str = "direct"
    enabled: bool = True
    focus: bool = True
    protected: str | None = None


CASES = (
    Case("direct_off", enabled=False), Case("direct_on"),
    Case("code_off", mode="code", enabled=False), Case("code_on", mode="code"),
    Case("poll_off", mode="poll", enabled=False), Case("poll_on", mode="poll"),
    Case("missing_focus", focus=False), Case("task_protected", protected="task"),
    Case("help_protected", protected="help"),
)


def windows_shell() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


WINDOWS_PRIVATE_CANARY = b"synthetic private denied read"
WINDOWS_PROBE_SCHEMA = "bello.windows-native-acl-probe.v1"
WINDOWS_PROBE_FIELDS = {"inside_write_succeeded", "outside_public_read_succeeded",
                        "outside_write_succeeded", "outside_private_read_succeeded"}


def create_windows_private_canary(path: Path) -> str:
    """Create only a fresh private fixture; never repair existing/parent ACLs."""
    from supervisor.runtime.native_codex_install import _windows_private_acl
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Private Windows fixture must be new")
    _windows_private_acl(path, create=True)
    secret = path / "secret.txt"
    with secret.open("xb") as stream:
        stream.write(WINDOWS_PRIVATE_CANARY)
    _windows_private_acl(secret)
    return file_sha256(secret)


def windows_filesystem_probe(outside: Path, private: Path) -> str:
    """Emit no output unless a real allowed/denied filesystem probe fails."""
    def literal(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"
    public_read = literal(outside / "secret.txt")
    private_read = literal(private / "secret.txt")
    denied_write = literal(outside / "forbidden.txt")
    # Only AccessDenied counts: a missing/bad fixture path must fail the proof.
    catch = "catch { if (-not ($_.Exception.GetBaseException() -is [UnauthorizedAccessException])) { throw } }; "
    return (
        "$ErrorActionPreference='Stop'; "
        "[IO.File]::WriteAllText((Join-Path (Get-Location).Path 'inside-write.txt'), 'allowed'); "
        "$publicReadable=$false; try { [void][IO.File]::ReadAllText(" + public_read + "); $publicReadable=$true } " + catch +
        "$writable=$false; try { [IO.File]::WriteAllText(" + denied_write + ", 'forbidden'); $writable=$true } " + catch +
        "$privateReadable=$false; try { [void][IO.File]::ReadAllText(" + private_read + "); $privateReadable=$true } " + catch +
        "$probe=@{schema='" + WINDOWS_PROBE_SCHEMA + "'; inside_write_succeeded=$true; "
        "outside_public_read_succeeded=$publicReadable; outside_write_succeeded=$writable; "
        "outside_private_read_succeeded=$privateReadable}; "
        "[IO.File]::WriteAllText((Join-Path (Get-Location).Path 'windows-filesystem-probe.json'), "
        "($probe | ConvertTo-Json -Compress)); "
        "if ($privateReadable -or $writable) { throw ('Windows native ACL sandbox probe failed: "
        "outside_public_read_succeeded={0}; outside_write_succeeded={1}; "
        "outside_private_read_succeeded={2}' -f $publicReadable,$writable,$privateReadable) }; "
    )


def windows_filesystem_result(output: Path, private_before_sha256: str, *, exact: bool) -> dict[str, Any]:
    """Validate the recorded probe, without claiming public-path read isolation."""
    result = {"windows_filesystem_contract": "native-acl-private-file-isolation-v1",
              "windows_arbitrary_public_path_read_confinement": "not-covered",
              "windows_filesystem_probe": None, "windows_filesystem_probe_error": None,
              "windows_private_fixture_sha256": {"before": private_before_sha256, "after": None},
              "windows_filesystem_sandbox_enforced": False}
    try:
        def fixture(path: str) -> bytes:
            target = output / path
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Invalid Windows probe fixture")
            return target.read_bytes()
        raw = fixture("work/windows-filesystem-probe.json")
        if len(raw) > 4096:
            raise ValueError("Oversized Windows probe JSON")
        probe = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(probe, dict) or set(probe) != WINDOWS_PROBE_FIELDS | {"schema"}
                or probe["schema"] != WINDOWS_PROBE_SCHEMA
                or any(type(probe[key]) is not bool for key in WINDOWS_PROBE_FIELDS)):
            raise ValueError("Malformed Windows probe JSON")
        result["windows_filesystem_probe"] = probe
        private = fixture("private-outside-workspace/secret.txt")
        private_after_sha256 = hashlib.sha256(private).hexdigest()
        result["windows_private_fixture_sha256"]["after"] = private_after_sha256
        result["windows_filesystem_sandbox_enforced"] = bool(
            exact and probe["inside_write_succeeded"]
            and not probe["outside_write_succeeded"] and not probe["outside_private_read_succeeded"]
            and fixture("work/inside-write.txt") == b"allowed"
            and fixture("outside-workspace/secret.txt") == b"synthetic denied read"
            and not os.path.lexists(output / "outside-workspace/forbidden.txt")
            and private == WINDOWS_PRIVATE_CANARY and private_after_sha256 == private_before_sha256)
    except (OSError, ValueError, UnicodeError) as exc:
        result["windows_filesystem_probe_error"] = type(exc).__name__ + ": invalid or missing synthetic probe/fixture"
    return result


def _windows_fixture_sddl(path: Path) -> str:
    """Read owner, group and DACL only; never read file data or request SACL."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        pointer, pointer, pointer, pointer, ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [pointer, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(pointer), pointer]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    descriptor, text = pointer(), pointer()
    try:
        status = advapi.GetNamedSecurityInfoW(str(path), 1, 0x00000007, None, None, None, None,
                                             ctypes.byref(descriptor))
        if status:
            raise ctypes.WinError(status)
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 0x00000007, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.wstring_at(text)
    finally:
        if text:
            kernel.LocalFree(text)
        if descriptor:
            kernel.LocalFree(descriptor)


def windows_fixture_acls(output: Path) -> dict[str, Any]:
    """Host-only diagnostics of fixed synthetic fixtures, never Codex home/auth."""
    fixtures = ("work", "work/diagnostic.log", "work/inside-write.txt",
                "outside-workspace", "outside-workspace/secret.txt", "outside-workspace/forbidden.txt",
                "private-outside-workspace", "private-outside-workspace/secret.txt")
    def redirected(status) -> bool:
        return bool(stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    def error_details(error: OSError) -> dict[str, Any]:
        # Error text can include paths; these diagnostic fields never do.
        return {"error": type(error).__name__, "winerror": getattr(error, "winerror", None),
                "errno": error.errno}
    # Do not follow a redirected container to a non-fixture object's ACL.
    try:
        for label in (".", "work", "outside-workspace", "private-outside-workspace"):
            status = (output / label).lstat()
            if redirected(status) or not stat.S_ISDIR(status.st_mode):
                return {"error": "redirected_fixture_container", "fixture": label}
    except OSError as error:
        return error_details(error)
    result = {}
    for label in fixtures:
        path = output / label
        try:
            status = path.lstat()
            if redirected(status):
                result[label] = {"exists": True, "error": "redirected_fixture"}
            else:
                result[label] = {"exists": True, "sddl": _windows_fixture_sddl(path)}
        except FileNotFoundError:
            result[label] = {"exists": False}
        except OSError as error:
            result[label] = error_details(error)
    return result


def windows_permission_params(work: Path, home: Path, binary: Path) -> dict[str, Any]:
    from supervisor.runtime.codex_permissions import native_permission_params
    result = native_permission_params(
        {"cwd": str(work), "sandbox": "workspace-write", "networkAccess": False},
        temp_dir=home / "tmp", runtime_read_paths=(binary,))
    result["config"]["windows"] = {"sandbox": "elevated"}
    return result


async def terminate_windows_setup(process, env: dict[str, str]) -> None:
    """Boundedly stop this live setup process and its Windows helper children."""
    if process.returncode is not None:
        return
    killer = None
    try:
        system_root = Path(env.get("SystemRoot", r"C:\Windows"))
        if not system_root.is_absolute():
            raise RuntimeError("Cannot stop Windows setup tree: SystemRoot is not absolute")
        async with asyncio.timeout(5):
            killer = await asyncio.create_subprocess_exec(
                str(system_root / "System32" / "taskkill.exe"), "/T", "/F", "/PID", str(process.pid),
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            status = await killer.wait()
        if status != 0:
            raise RuntimeError(f"Windows setup tree cleanup failed with exit code {status}")
    finally:
        try:
            if killer is not None and killer.returncode is None:
                killer.kill()
                await asyncio.wait_for(killer.wait(), 1)
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)


async def provision_windows_sandbox(binary: Path, home: Path, env: dict[str, str], output: Path) -> None:
    # CI only, on its disposable elevated runner. No automatic UAC or inherited
    # account credentials. The setup state stays outside uploaded diagnostics.
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        raise RuntimeError("Windows proof requires the runner's USERNAME for native sandbox setup")
    setup_env = {key: value for key, value in env.items() if not key.upper().startswith("BELLO_SELECTOR_")}
    process = await asyncio.create_subprocess_exec(
        str(binary), "sandbox", "setup", "--elevated", "--current-user", "--codex-home", str(home),
        cwd=home, env={**setup_env, "USERNAME": username},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
    except (TimeoutError, asyncio.CancelledError) as error:
        try:
            await terminate_windows_setup(process, setup_env)
        except (Exception, asyncio.CancelledError) as cleanup_error:
            error.add_note(f"Windows setup cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}")
        if isinstance(error, TimeoutError):
            raise RuntimeError("Native Windows sandbox setup timed out") from error
        raise
    (output / "windows-setup-stdout.txt").write_bytes(stdout)
    (output / "windows-setup-stderr.txt").write_bytes(stderr)
    if process.returncode != 0:
        raise RuntimeError(f"Native Windows sandbox setup failed with exit code {process.returncode}")


def isolated_environment(home: Path, binary: Path, port: int,
                         bridge_environment: dict[str, str], *, windows: bool | None = None) -> dict[str, str]:
    windows = os.name == "nt" if windows is None else windows
    proxy = f"http://127.0.0.1:{port}"
    env = {
        "HOME": str(home), "CODEX_HOME": str(home),
        "PATH": str(binary.parent) + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(home / "tmp"), "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"), "SHELL": "/bin/bash", "RUST_LOG": "warn",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    if windows:
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        env.update({"SystemRoot": str(system), "WINDIR": str(system),
                    "USERPROFILE": str(home), "APPDATA": str(home / "config"),
                    "LOCALAPPDATA": str(home / "data"), "COMSPEC": str(system / "System32" / "cmd.exe"),
                    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                    "PATH": ";".join(map(str, (binary.parent, system / "System32", Path(windows_shell()).parent)))})
        env.pop("SHELL")
        if os.environ.get("USERNAME", "").strip():
            env["USERNAME"] = os.environ["USERNAME"]
    allowed = {"BELLO_SELECTOR_SOCKET", "BELLO_SELECTOR_TCP", "BELLO_SELECTOR_TOKEN"}
    if set(bridge_environment) - allowed:
        raise ValueError("Unexpected native bridge environment")
    env.update(bridge_environment)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = proxy
    return env


def invocation(case: Case, *, windows: bool | None = None, command_prefix: str = "") -> tuple[dict[str, Any], str]:
    windows = os.name == "nt" if windows is None else windows
    command = {"task": "cat fixture-requirements.data; exit 7", "help": "bash fixture-help.sh --help; exit 7"}.get(
        case.protected, "cat diagnostic.log; exit 7")
    if case.mode == "poll":
        command = "sleep 2; " + command
    if windows:
        command = {
            "task": "[Console]::Out.Write((Get-Content -Raw -LiteralPath 'fixture-requirements.data')); exit 7",
            # Inline fixture: independent of .ps1 execution policy and native
            # child-output newline conversion by Windows PowerShell.
            "help": "function fixture-help { [Console]::Out.Write([IO.File]::ReadAllText('diagnostic.log')) }; fixture-help --help; exit 7",
        }.get(case.protected, "[Console]::Out.Write((Get-Content -Raw -LiteralPath 'diagnostic.log')); exit 7")
        if case.mode == "poll":
            # Pinned native Windows exec clamps the initial yield to 10s.
            # Keep the fixture alive beyond that floor to exercise write_stdin.
            command = "Start-Sleep -Seconds 15; " + command
    command = command_prefix + command
    args: dict[str, Any] = {"cmd": command, "yield_time_ms": 250 if case.mode == "poll" else 1000,
                           "max_output_tokens": 10000, "login": False}
    if windows:
        args["shell"] = windows_shell()
    if case.focus:
        args["focus"] = FOCUS
    if case.mode == "direct":
        return {"type": "function_call", "call_id": CALL_ID, "name": "exec_command",
                "arguments": json.dumps(args)}, command
    code = "let r = await tools.exec_command(" + json.dumps(args) + ");\n"
    if case.mode == "poll":
        poll: dict[str, Any] = {"chars": "", "yield_time_ms": 1000, "max_output_tokens": 10000}
        if case.focus:
            poll["focus"] = POLL_FOCUS
        code += 'if (typeof r.session_id !== "number") throw new Error("Missing live session");\n'
        code += "for (let n=0; n<10 && r.session_id; n++) {\n"
        code += "r = await tools.write_stdin({..." + json.dumps(poll) + ", session_id:r.session_id});\n"
        code += "if(r.output) text(r);\n}\n"
        code += 'if(r.session_id) throw new Error("Polling did not finish");'
    else:
        code += "text(r);"
    if windows and case.mode == "poll":
        # Code mode itself otherwise yields after 10s, before this fixture can
        # finish. The fake provider intentionally has only one tool turn.
        code = '// @exec: {"yield_time_ms":30000}\n' + code
    return {"type": "custom_tool_call", "call_id": CALL_ID, "name": "exec", "input": code}, command


def response_events(index: int, tool: dict[str, Any]) -> bytes:
    response_id = f"fixture-response-{index}"
    item = tool if index == 1 else {"type": "message", "id": "fixture-final", "role": "assistant",
        "content": [{"type": "output_text", "text": "Synthetic fixture complete."}]}
    events = [{"type": "response.created", "response": {"id": response_id}},
              {"type": "response.output_item.done", "item": item},
              {"type": "response.completed", "response": {"id": response_id, "usage": {
                  "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                  "input_tokens_details": None, "output_tokens_details": None}}}]
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()


def decode_body(body: bytes, encoding: str) -> dict[str, Any]:
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "zstd":
        try:
            from compression import zstd
            body = zstd.decompress(body)
        except ImportError:
            import zstandard
            body = zstandard.ZstdDecompressor().decompress(body, max_output_size=LIMIT)
    elif encoding not in ("", "identity"):
        raise ValueError("Unexpected request encoding: " + encoding)
    if len(body) > LIMIT:
        raise ValueError("Provider request exceeds fixture limit")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("Provider request is not an object")
    return result


class Provider:
    def __init__(self, tool: dict[str, Any]):
        self.tool = tool
        self.requests: list[dict[str, Any]] = []
        self.rejected: list[str] = []
        self.errors: list[str] = []

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reject(self):
                owner.rejected.append(self.command + " " + self.path)
                self.send_error(403, "Offline fixture: no external or unknown endpoints")

            do_CONNECT = do_GET = do_PUT = reject

            def do_POST(self):
                if self.path != "/v1/responses":
                    self.reject()
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= LIMIT:
                        raise ValueError("Invalid request body size")
                    value = decode_body(self.rfile.read(size), self.headers.get("Content-Encoding", ""))
                    if self.headers.get("Authorization") or self.headers.get("Cookie"):
                        raise ValueError("Unexpected credentials at offline provider")
                    owner.requests.append(value)
                    if len(owner.requests) > 2:
                        raise ValueError("Unexpected third model request")
                    body = response_events(len(owner.requests), owner.tool)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    owner.errors.append(type(exc).__name__ + ": " + str(exc))
                    self.send_error(500, "Offline fixture failed")

        return Handler


class NativeSession:
    def __init__(self, binary: Path, home: Path, env: dict[str, str]):
        self.binary, self.home, self.env = binary, home, env
        self.process = None
        self.pending: dict[int, asyncio.Future] = {}
        self.transcript: list[dict[str, Any]] = []
        self.notifications: asyncio.Queue = asyncio.Queue()
        self.counter = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(str(self.binary), "app-server", "--listen", "stdio://",
            cwd=self.home, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LIMIT)
        self.reader = asyncio.create_task(self.read())
        self.stderr = asyncio.create_task(self.process.stderr.read(LIMIT))
        await self.request("initialize", {"clientInfo": {"name": "bello_selection_fixture", "version": "1.0"},
                                           "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized", "params": {}})

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                self.transcript.append(message)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(json.dumps(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    await self.server_request(message)
                else:
                    await self.notifications.put(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Native app-server reader ended"))

    async def server_request(self, message):
        await self.send({"id": message["id"], "error": {"code": -32600,
                              "message": "Unexpected server request in offline fixture"}})
        await self.notifications.put({"method": "fixture/error", "params": message})

    async def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)

    async def complete(self, thread_id: str, *, timeout: float = 60):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.notifications.get()
                if event.get("method") == "fixture/error":
                    raise RuntimeError("Native fixture received unexpected approval/tool request")
                if event.get("method") == "turn/completed" and event.get("params", {}).get("threadId") == thread_id:
                    turn = event["params"]["turn"]
                    if turn.get("status") != "completed":
                        raise RuntimeError("Native turn did not complete: " + json.dumps(turn))
                    return

    async def close(self, output: Path):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        await asyncio.gather(self.reader, return_exceptions=True)
        output.joinpath("native-stderr.txt").write_bytes(await self.stderr)
        output.joinpath("rpc.jsonl").write_text("".join(json.dumps(v) + "\n" for v in self.transcript))


def output_packets(request: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    kind = "function_call_output" if mode == "direct" else "custom_tool_call_output"
    packets = []
    for item in request.get("input", []):
        if item.get("type") != kind or item.get("call_id") != CALL_ID:
            continue
        value = item.get("output")
        texts = [value] if isinstance(value, str) else [v.get("text") for v in value or [] if isinstance(v, dict)]
        for text in texts:
            if not isinstance(text, str):
                continue
            if mode == "direct":
                prefix, separator, payload = text.partition("\nOutput:\n")
                code = re.search(r"(?m)^Process exited with code (-?\d+)$", prefix)
                if separator and code:
                    packets.append({"output": payload, "exit_code": int(code[1])})
            else:
                # Code mode wraps text(r) in native timing text. Decode complete
                # JSON objects, never confuse escaped log text with its metadata.
                decoder, offset = json.JSONDecoder(), 0
                while (start := text.find("{", offset)) >= 0:
                    try:
                        value, consumed = decoder.raw_decode(text[start:])
                    except ValueError:
                        offset = start + 1
                        continue
                    offset = start + consumed
                    if isinstance(value, dict) and "output" in value:
                        packets.append(value)
    return packets


async def run_case(binary: Path, case: Case, output: Path, *, selector=None,
                   raw_output: str = RAW, turn_timeout: float = 60,
                   async_tools: bool = False) -> dict[str, Any]:
    from supervisor.runtime.codex_distiller import CodexDistillerBridge

    output.mkdir(parents=True, exist_ok=False)
    work, home = output / "work", output / "empty-home"
    work.mkdir()
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir()
    for name in ("diagnostic.log", "fixture-requirements.data"):
        (work / name).write_text(raw_output, encoding="utf-8", newline="\n")
    (work / "fixture-help.sh").write_text("#!/bin/bash\ncat diagnostic.log\n")
    outside = output / "outside-workspace"
    private = output / "private-outside-workspace"
    private_before_sha256 = None
    if os.name == "nt":
        outside.mkdir()
        (outside / "secret.txt").write_text("synthetic denied read", encoding="ascii")
        private_before_sha256 = create_windows_private_canary(private)
    selections = []

    class RecordingSelector:
        async def distill(self, log, focus, command):
            record = {"log": log, "focus": focus, "command": command}
            selections.append(record)
            if log != raw_output:
                raise ValueError("Unexpected synthetic input")
            result = SELECTED if selector is None else await selector.distill(log, focus, command)
            record["selected"] = result
            return result

    # A custom filename ensures this tests exact registered task scope, not just
    # the generic TASK.md/README name exclusion.
    bridge = CodexDistillerBridge(RecordingSelector(), output / "bridge", work, work / "fixture-requirements.data")
    await bridge.start()
    tool, command = invocation(case, command_prefix=windows_filesystem_probe(outside, private) if os.name == "nt" else "")
    provider = Provider(tool)
    server = ThreadingHTTPServer(("127.0.0.1", 0), provider.handler())
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_port
    (home / "config.toml").write_text(
        'model_provider = "bello_fixture"\ncli_auth_credentials_store = "file"\n'
        '[features]\nenable_request_compression = false\n'
        '[model_providers.bello_fixture]\nname = "Offline fixture"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
        'request_max_retries = 0\nstream_max_retries = 0\n')
    session = NativeSession(binary, home, isolated_environment(home, binary, port, bridge.environment))
    failure = None
    windows_acls = None
    try:
        if os.name == "nt":
            windows_acls = {"before": windows_fixture_acls(output)}
            await provision_windows_sandbox(binary, home, session.env, output)
        await session.start()
        params = {"model": "gpt-5.5" if case.mode == "direct" else "gpt-6-astra",
            "modelProvider": "bello_fixture", "cwd": str(work), "approvalPolicy": "never", "sandbox": "read-only",
            "ephemeral": True, "developerInstructions": "Add a short focus to each command or poll call.",
            "config": {"features.bello_native_selection": case.enabled, "features.code_mode": case.mode != "direct",
                       "features.code_mode_only": case.mode != "direct", "features.shell_zsh_fork": False}}
        if async_tools:
            params["config"]["features.bello_async_tools"] = True
        if os.name == "nt":
            permissions = windows_permission_params(work, home, binary)
            params["config"].update(permissions.pop("config"))
            params.update(permissions)
            params.pop("sandbox")
        reply = await session.request("thread/start", params)
        thread_id = reply["thread"]["id"]
        await session.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "Run the synthetic local fixture.", "text_elements": []}]})
        await session.complete(thread_id, timeout=turn_timeout)
    except Exception as exc:
        failure = type(exc).__name__ + ": " + str(exc)
    finally:
        await session.close(output)
        if os.name == "nt" and windows_acls is not None:
            windows_acls["after"] = windows_fixture_acls(output)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        serving.join(timeout=5)
        await bridge.close()
    for index, request in enumerate(provider.requests, 1):
        (output / f"provider-request-{index}.json").write_text(json.dumps(request, indent=2) + "\n")
    (output / "selector-inputs.json").write_text(json.dumps(selections, indent=2) + "\n")
    packets = output_packets(provider.requests[1], case.mode) if len(provider.requests) == 2 else []
    selected = case.enabled and case.focus and not case.protected
    expected = SELECTED if selected else raw_output
    if selected and selector is not None:
        expected = selections[0].get("selected") if len(selections) == 1 else None
    exact = (isinstance(expected, str) and len(packets) == 1
             and packets[0].get("output") == expected and packets[0].get("exit_code") == 7)
    windows_isolated = None
    windows_probe_result = {}
    if os.name == "nt":
        windows_probe_result = windows_filesystem_result(output, private_before_sha256, exact=exact)
        windows_isolated = windows_probe_result["windows_filesystem_sandbox_enforced"]
    focus_ok = (len(selections) == 1 and selections[0]["focus"] == (POLL_FOCUS if case.mode == "poll" else FOCUS)
                and selections[0]["command"] == command) if selected else not selections
    # Native startup may attempt update/catalog discovery. The loopback proxy
    # rejects and records these; a blocked attempt is not an external request
    # succeeding, and is not evidence of output selection failing.
    result = {"case": case.name, "passed": not failure and exact and focus_ok and not provider.errors and windows_isolated is not False,
        "error": failure, "exact_model_visible_output": exact, "focus_and_command_correct": focus_ok,
        "provider_requests": len(provider.requests), "selector_calls": len(selections),
        "bridge_outcomes": dict(bridge.metrics), "rejected_network_requests": provider.rejected,
        "provider_errors": provider.errors, "external_proxy_requests_forwarded": 0,
        "windows_filesystem_sandbox_enforced": windows_isolated,
        "windows_fixture_acls": windows_acls,
        "expected_output_bytes": len(expected.encode()) if isinstance(expected, str) else None,
        "actual_output_bytes": [len(str(p.get("output", "")).encode()) for p in packets]}
    result.update(windows_probe_result)
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


async def main_async(args) -> int:
    binary, output = args.codex.resolve(strict=True), args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=False)
    chosen = [case for case in CASES if not args.cases or case.name in args.cases.split(",")]
    if not chosen:
        raise ValueError("No recognized cases selected")
    results = []
    for case in chosen:
        result = await run_case(binary, case, output / case.name)
        results.append(result)
        print(json.dumps(result), flush=True)
    report = {"schema": "bello.native-selection-provider-proof.v1", "paid_model_calls": 0,
        "selector": "deterministic synthetic double; real native/Python bridge",
        "binary": str(binary), "binary_sha256": file_sha256(binary),
        "platform": platform.platform(), "machine": platform.machine(),
        "script_sha256": file_sha256(Path(__file__)),
        "source_files": {name: file_sha256(ROOT / "supervisor" / "runtime" / name)
                         for name in ("codex_distiller.py", "distiller_policy.py")},
        "cases": results, "passed": all(result["passed"] for result in results),
        "not_covered": ["learned selector quality/latency", "automatic native child-agent inheritance",
                        "Windows arbitrary public-path read confinement"]}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for synthetic proof artifacts")
    parser.add_argument("--cases", help="Comma-separated subset; by default all nine cases")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
