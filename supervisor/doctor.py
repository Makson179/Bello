from __future__ import annotations

import asyncio
import json
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from supervisor.appserver import AppServerClient, CODEX_NO_WEB_SEARCH_CONFIG_FLAGS
from supervisor.executables import resolve_trusted_executable
from supervisor import update_check


APP_SERVER_REQUIRED_SCHEMA_FILES = (
    "ClientRequest.json",
    "ServerRequest.json",
    "TurnStartParams.json",
    "CommandExecutionRequestApprovalParams.json",
)


DoctorLevel = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class DoctorResult:
    level: DoctorLevel
    message: str
    detail: str | None = None


def run_doctor() -> int:
    results = collect_doctor_results()
    print("Bello doctor")
    print()
    for result in results:
        print(format_result(result))
        if result.detail:
            print(f"  {result.detail}")
    return 1 if any(result.level == "fail" for result in results) else 0


def collect_doctor_results() -> list[DoctorResult]:
    results: list[DoctorResult] = []
    results.append(_python_version_result())
    results.extend(_platform_results())

    git_path = _doctor_executable("git")
    results.append(
        DoctorResult("ok", f"Git found: {git_path}")
        if git_path
        else DoctorResult("fail", "Git not found on PATH", _missing_git_detail())
    )

    results.extend(_runtime_dependency_results())

    info = update_check.read_install_info()
    if not info.metadata_available:
        results.append(DoctorResult("fail", "Bello package metadata could not be read", info.warning))
    else:
        results.append(DoctorResult("ok", f"Bello package: {info.package_name} {info.version}"))

    executable = _doctor_executable("bello")
    if executable:
        results.append(DoctorResult("ok", f"Bello executable: {executable}"))
    else:
        detail = "Close and reopen the terminal after installation so PATH changes take effect." if _is_windows() else None
        results.append(DoctorResult("warn", "bello command not found on PATH", detail))
    results.append(DoctorResult("ok", f"Bello install mode: {info.install_mode}"))

    if update_check.skip_update_check_enabled():
        results.append(
            DoctorResult(
                "ok",
                "Bello update check skipped",
                f"{update_check.SKIP_UPDATE_CHECK_ENV}=1",
            )
        )
    else:
        status = update_check.check_for_update(info)
        if status.state == update_check.UpdateState.CURRENT:
            results.append(DoctorResult("ok", "Bello is up to date"))
        elif status.state == update_check.UpdateState.OUTDATED:
            results.append(
                DoctorResult(
                    "warn",
                    f"Update available: {status.latest_version}",
                    "Run: bello update",
                )
            )
        else:
            results.append(DoctorResult("warn", "Could not check for Bello updates", status.warning))

    return results


def _runtime_dependency_results() -> list[DoctorResult]:
    from supervisor.appserver import AppServerError
    from supervisor.runtime.claude import ClaudeBackend
    from supervisor.runtime.install import node_executable, worker_command

    results: list[DoctorResult] = []
    try:
        node = node_executable()
        results.append(DoctorResult("ok", f"Node.js supported: {node}"))
        worker_command()
        results.append(DoctorResult("ok", "Pinned Pi runtime installed"))
    except Exception as exc:
        results.append(DoctorResult("fail", "Pi runtime dependency check failed", str(exc)))
    results.append(_sandbox_dependency_result())
    try:
        claude = ClaudeBackend._bundled_cli_path()
    except (AppServerError, OSError) as exc:
        results.append(DoctorResult(
            "warn", "Claude Code subscription backend is not installed or is incomplete",
            f"{exc}. Install Bello with the optional `claude` extra to use this backend. "
            "A standalone claude on PATH does not replace the SDK bundle. Not required for Pi/API providers.",
        ))
    else:
        results.append(DoctorResult(
            "ok", f"Official Claude Code SDK bundle found: {claude}",
            "Optional subscription backend; authenticate with `bello runtime login claude-code`.",
        ))
    return results


def _sandbox_dependency_result() -> DoctorResult:
    from supervisor.runtime import sandbox, windows_sandbox

    system = platform.system()
    try:
        if system == "Linux":
            backend = sandbox._linux_launcher()
        elif system == "Darwin":
            backend = sandbox._trusted_launcher(Path("/usr/bin/sandbox-exec"), "macOS sandbox-exec")
        elif system == "Windows":
            # Check the installed helper without executing it or changing ACLs.
            # The actual run preflight checks the writable workspace authority.
            backend = windows_sandbox._helper_path(Path.cwd(), "read-only")
        else:
            raise sandbox.SandboxUnavailableError(f"Unsupported sandbox platform: {system}")
    except (sandbox.SandboxUnavailableError, windows_sandbox.WindowsSandboxUnavailableError, OSError) as exc:
        detail = str(exc)
        if system == "Linux":
            detail += f". {_bubblewrap_install_hint()}"
        detail += ". Bello will not silently run outside its configured sandbox."
        return DoctorResult("fail", "No supported OS sandbox is available", detail)
    detail = "The run preflight also checks whether the sandbox can actually start with the requested workspace permissions."
    if system == "Windows":
        detail += (
            " Check one-time host preparation with `bello runtime windows-sandbox status`. "
            "If needed, run `bello runtime windows-sandbox prepare` in an administrator terminal; "
            "normal tasks do not require elevation. Check NUL device access separately with "
            "`bello runtime windows-sandbox status --null-device`; if missing, explicitly run "
            "`bello runtime windows-sandbox prepare --null-device` as administrator. "
            "Windows resets the NUL permission on reboot. Offline runs additionally require "
            "`bello runtime windows-sandbox prepare --network` once as administrator; "
            "inspect that fixed service with `bello runtime windows-sandbox status --network`."
        )
    return DoctorResult("ok", f"OS sandbox executable found: {backend}", detail)


def _bubblewrap_install_hint() -> str:
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    families = [release.get("ID", ""), *release.get("ID_LIKE", "").split()]
    commands = {
        "debian": "sudo apt install bubblewrap",
        "ubuntu": "sudo apt install bubblewrap",
        "fedora": "sudo dnf install bubblewrap",
        "rhel": "sudo dnf install bubblewrap",
        "arch": "sudo pacman -S bubblewrap",
        "opensuse": "sudo zypper install bubblewrap",
        "alpine": "sudo apk add bubblewrap",
    }
    command = next((commands[family] for family in families if family in commands), None)
    action = f"Install the system package with: {command}" if command else (
        "Install the bubblewrap system package using your distribution's package manager"
    )
    return f"{action}. This is a suggestion only; Bello does not install system packages automatically"


def format_result(result: DoctorResult) -> str:
    marker = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}[result.level]
    return f"{marker} {result.message}"


def _python_version_result() -> DoctorResult:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        return DoctorResult("ok", f"Python {version}")
    detail = "Install Python 3.11 or newer from python.org and reopen the terminal." if _is_windows() else "Python 3.11 or newer is required"
    return DoctorResult("fail", f"Python {version}", detail)


def _is_windows() -> bool:
    return sys.platform == "win32" or platform.system().casefold() == "windows"


def _doctor_executable(name: str) -> str | None:
    # Test doubles can emulate Windows reporting on another host, but strict
    # CreateProcess resolution is required only on a real native-Windows
    # interpreter.
    if sys.platform != "win32":
        return shutil.which(name)
    return resolve_trusted_executable(name, cwd=Path.cwd(), windows=True)


def _platform_results() -> list[DoctorResult]:
    system = platform.system() or "unknown"
    release = platform.release() or "unknown release"
    version = platform.version()
    if not _is_windows():
        return [DoctorResult("ok", f"Platform: {system} {release}")]
    return [
        _windows_version_result(release, version),
        _windows_architecture_result(),
        _windows_shell_result(),
    ]


def _windows_version_result(release: str, version: str) -> DoctorResult:
    build = _windows_build_number(version)
    product_type: int | None = None
    getwindowsversion = getattr(sys, "getwindowsversion", None)
    if callable(getwindowsversion):
        try:
            native_version = getwindowsversion()
            build = int(native_version.build)
            product_type = int(native_version.product_type)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    release_key = release.casefold().replace(" ", "")
    server_release = "server" in release_key
    is_server = (product_type is not None and product_type != 1) or server_release
    if is_server:
        supported = build is not None and build >= 20348
        if build is None:
            supported = release_key in {"2022server", "server2022", "2025server", "server2025"}
    else:
        supported = release_key == "11" or (build is not None and build >= 22000)

    detail = f"build {version}" if version else None
    if supported:
        return DoctorResult("ok", f"Native Windows detected: {release}", detail)
    detected = f"; detected build {build}" if build is not None else ""
    return DoctorResult(
        "fail",
        f"Unsupported native Windows version: {release}",
        "Bello supports Windows 11 or Windows Server 2022/2025"
        f"{detected}. Upgrade Windows or use the WSL installation path.",
    )


def _windows_build_number(version: str) -> int | None:
    numbers = [int(value) for value in version.split(".") if value.isdigit()]
    return numbers[-1] if numbers else None


def _windows_architecture_result() -> DoctorResult:
    machine = (platform.machine() or "unknown").strip()
    machine_key = machine.casefold()
    bits = struct.calcsize("P") * 8
    supported_machine = machine_key in {"amd64", "x64", "x86_64"}
    if bits == 64 and supported_machine:
        return DoctorResult("ok", f"Windows architecture: {machine} ({bits}-bit Python)")
    return DoctorResult(
        "fail",
        f"Unsupported Windows architecture: {machine} ({bits}-bit Python)",
        "Install 64-bit x86 Windows and a 64-bit Python build. Windows on ARM and 32-bit Python are not supported.",
    )


def _windows_shell_result() -> DoctorResult:
    for executable in ("pwsh", "powershell", "cmd"):
        path = _doctor_executable(executable)
        if path:
            return DoctorResult("ok", f"Windows shell found: {path}")
    return DoctorResult(
        "fail",
        "No supported Windows shell found on PATH",
        "Install PowerShell 7 (pwsh) or restore Windows PowerShell/cmd.exe, then reopen the terminal.",
    )


def _missing_git_detail() -> str:
    if _is_windows():
        return "Install Git for Windows, select the option to add Git to PATH, then reopen the terminal."
    return "Install Git and ensure the git executable is on PATH."


def _missing_codex_detail() -> str:
    if _is_windows():
        return "Install the native Codex CLI, ensure codex.exe or codex.cmd is on PATH, then run: codex login"
    return "Install the Codex CLI, ensure it is on PATH, then run: codex login"


def _probe_result(args: list[str], ok_message: str, fail_message: str, *, timeout: float = 10.0) -> DoctorResult:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as exc:
        return DoctorResult("fail", fail_message, str(exc))
    except subprocess.TimeoutExpired:
        return DoctorResult("fail", fail_message, f"{args[0]} timed out")
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode == 0:
        detail = output.splitlines()[0] if output else None
        return DoctorResult("ok", ok_message, detail)
    return DoctorResult("fail", fail_message, output or f"exit code {completed.returncode}")


def _schema_generation_result(codex_executable: str | None = None) -> DoctorResult:
    with tempfile.TemporaryDirectory(prefix="bello-doctor-schema-") as tmp_dir:
        out_dir = Path(tmp_dir)
        try:
            codex_executable = codex_executable or _doctor_executable("codex")
            if codex_executable is None:
                return DoctorResult(
                    "fail",
                    "app-server schema generation failed",
                    "trusted Codex executable not found on PATH",
                )
            completed = subprocess.run(
                [codex_executable, "app-server", "generate-json-schema", "--experimental", "--out", str(out_dir)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DoctorResult("fail", "app-server schema generation failed", str(exc))
        if completed.returncode != 0:
            return DoctorResult("fail", "app-server schema generation failed", (completed.stdout + completed.stderr).strip())

        for name in APP_SERVER_REQUIRED_SCHEMA_FILES:
            path = _schema_file(out_dir, name)
            if path is None:
                return DoctorResult("fail", f"app-server schema missing required file: {name}")
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                return DoctorResult("fail", f"app-server schema file unreadable: {name}", str(exc))
    return DoctorResult("ok", "app-server schema generation OK")


def _schema_file(out_dir: Path, name: str) -> Path | None:
    direct = out_dir / name
    if direct.exists():
        return direct
    nested = out_dir / "v2" / name
    if nested.exists():
        return nested
    return None


def _codex_auth_result(codex_executable: str | None = None) -> DoctorResult:
    async def probe() -> DoctorResult:
        command = None
        if codex_executable is not None:
            command = [
                codex_executable,
                "app-server",
                *CODEX_NO_WEB_SEARCH_CONFIG_FLAGS,
                "--listen",
                "stdio://",
            ]
        client = AppServerClient(command=command)
        try:
            await asyncio.wait_for(client.start(), timeout=10)
            await client.initialize(timeout=10)
            account = await client.account_read(timeout=10)
            await client.config_requirements_read(timeout=10)
        except Exception as exc:
            return DoctorResult("fail", "Codex auth check failed", str(exc))
        finally:
            await client.stop()
        if account.get("requiresOpenaiAuth") and account.get("account") is None:
            return DoctorResult("fail", "Codex auth missing", "Run: codex login")
        return DoctorResult("ok", "Codex auth OK")

    return asyncio.run(probe())
