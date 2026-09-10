from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from supervisor import doctor
from supervisor.appserver import AppServerError
from supervisor.runtime import install, sandbox, windows_sandbox
from supervisor.runtime.claude import ClaudeBackend


@pytest.fixture(autouse=True)
def isolated_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exercise doctor itself without launching tools, installers, or providers."""
    monkeypatch.setattr(install, "node_executable", lambda: "/trusted/node")
    monkeypatch.setattr(install, "worker_command", lambda: ["/trusted/node", "worker.mjs"])
    monkeypatch.setattr(doctor.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(doctor.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})

    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(tmp_path / "claude_agent_sdk" / "__init__.py")
    bundle = Path(sdk.__file__).parent / "_bundled"
    bundle.mkdir(parents=True)
    (bundle / ("claude.exe" if os.name == "nt" else "claude")).write_text("fixture")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Dependency diagnostics must not start subprocesses or query models")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def sandbox_result() -> doctor.DoctorResult:
    return next(result for result in doctor._runtime_dependency_results() if "OS sandbox" in result.message)


def claude_result() -> doctor.DoctorResult:
    return next(result for result in doctor._runtime_dependency_results() if "Claude Code" in result.message)


def test_trusted_linux_bubblewrap_does_not_require_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    result = sandbox_result()
    assert result.level == "ok"
    assert "/usr/bin/bwrap" in result.message
    assert "preflight" in result.detail


def test_untrusted_path_bubblewrap_does_not_count_as_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_doctor_executable", lambda _name: "/untrusted/bin/bwrap")

    def unavailable() -> Path:
        raise sandbox.SandboxUnavailableError("No trusted system bubblewrap installation")

    monkeypatch.setattr(sandbox, "_linux_launcher", unavailable)
    result = sandbox_result()
    assert result.level == "fail"
    assert "No trusted system bubblewrap" in result.detail
    assert "sudo apt install bubblewrap" in result.detail
    assert "does not install system packages automatically" in result.detail
    assert "will not silently run outside" in result.detail


@pytest.mark.parametrize(
    ("release", "command"),
    [
        ({"ID": "ubuntu"}, "sudo apt install bubblewrap"),
        ({"ID": "debian"}, "sudo apt install bubblewrap"),
        ({"ID": "fedora"}, "sudo dnf install bubblewrap"),
        ({"ID": "rhel"}, "sudo dnf install bubblewrap"),
        ({"ID": "arch"}, "sudo pacman -S bubblewrap"),
        ({"ID": "opensuse"}, "sudo zypper install bubblewrap"),
        ({"ID": "alpine"}, "sudo apk add bubblewrap"),
        ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, "sudo apt install bubblewrap"),
        ({"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"}, "sudo zypper install bubblewrap"),
        ({"ID": "fedora", "ID_LIKE": "debian"}, "sudo dnf install bubblewrap"),
    ],
)
def test_bubblewrap_hint_uses_distribution_family(
    monkeypatch: pytest.MonkeyPatch, release: dict[str, str], command: str
) -> None:
    monkeypatch.setattr(doctor.platform, "freedesktop_os_release", lambda: release)
    hint = doctor._bubblewrap_install_hint()
    assert command in hint
    assert "suggestion only" in hint


@pytest.mark.parametrize("release", [{}, {"ID": "unknown", "ID_LIKE": "unknown-family"}])
def test_bubblewrap_hint_for_unknown_distribution(
    monkeypatch: pytest.MonkeyPatch, release: dict[str, str]
) -> None:
    monkeypatch.setattr(doctor.platform, "freedesktop_os_release", lambda: release)
    hint = doctor._bubblewrap_install_hint()
    assert "distribution's package manager" in hint
    assert "sudo" not in hint


def test_bubblewrap_hint_when_os_release_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> dict[str, str]:
        raise OSError("os-release unavailable")

    monkeypatch.setattr(doctor.platform, "freedesktop_os_release", unavailable)
    assert "distribution's package manager" in doctor._bubblewrap_install_hint()


def prepare_windows_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    module = tmp_path / "runtime" / "windows_sandbox.py"
    helper = module.parent / "bin" / "bello-windows-sandbox.exe"
    helper.parent.mkdir(parents=True)
    monkeypatch.setattr(windows_sandbox, "__file__", str(module))
    return helper


def test_packaged_windows_helper_is_detected_without_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = prepare_windows_package(monkeypatch, tmp_path)
    helper.write_text("not an executable; detection must not launch it")
    # Installation detection does not reject an otherwise valid helper merely
    # because doctor was invoked from the source tree containing the package.
    monkeypatch.chdir(tmp_path)
    result = sandbox_result()
    assert result.level == "ok"
    assert str(helper.resolve()) in result.message
    assert "requested workspace permissions" in result.detail


def test_missing_windows_helper_fails_with_packaged_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = prepare_windows_package(monkeypatch, tmp_path)
    result = sandbox_result()
    assert result.level == "fail"
    assert str(helper) in result.detail
    assert "bubblewrap" not in result.detail


def test_windows_helper_directory_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    helper = prepare_windows_package(monkeypatch, tmp_path)
    helper.mkdir()
    result = sandbox_result()
    assert result.level == "fail"
    assert "non-reparse regular file" in result.detail


def test_windows_helper_symlink_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    helper = prepare_windows_package(monkeypatch, tmp_path)
    target = tmp_path / "target.exe"
    target.write_text("fixture")
    try:
        helper.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"This account cannot create symlinks: {exc}")
    result = sandbox_result()
    assert result.level == "fail"
    assert "non-reparse regular file" in result.detail


def test_sdk_bundled_claude_is_detected_without_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    result = claude_result()
    assert result.level == "ok"
    assert "SDK bundle" in result.message
    assert "_bundled" in result.message
    assert "bello runtime login claude-code" in result.detail


def test_standalone_claude_does_not_replace_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_doctor_executable", lambda _name: "/standalone/claude")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    result = claude_result()
    assert result.level == "warn"
    assert "pinned claude-agent-sdk" in result.detail
    assert "standalone claude on PATH does not replace" in result.detail
    assert "Not required for Pi/API providers" in result.detail


def test_incomplete_sdk_bundle_is_optional_warning() -> None:
    ClaudeBackend._bundled_cli_path().unlink()
    result = claude_result()
    assert result.level == "warn"
    assert "bundled with claude-agent-sdk is missing" in result.detail


def test_unreadable_sdk_bundle_is_optional_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied() -> Path:
        raise PermissionError("SDK bundle inaccessible")

    monkeypatch.setattr(ClaudeBackend, "_bundled_cli_path", staticmethod(denied))
    result = claude_result()
    assert result.level == "warn"
    assert "SDK bundle inaccessible" in result.detail


@pytest.mark.parametrize("dependency", ["node_executable", "worker_command"])
def test_pi_failure_does_not_hide_sandbox_and_claude_results(
    monkeypatch: pytest.MonkeyPatch, dependency: str
) -> None:
    def unavailable() -> None:
        raise AppServerError("Pi dependency missing")

    monkeypatch.setattr(install, dependency, unavailable)
    results = doctor._runtime_dependency_results()
    assert any(result.level == "fail" and "Pi runtime" in result.message for result in results)
    assert any(result.level == "ok" and "OS sandbox" in result.message for result in results)
    assert any(result.level == "ok" and "Claude Code SDK bundle" in result.message for result in results)


def test_unsupported_platform_fails_without_linux_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "UnknownOS")
    result = sandbox_result()
    assert result.level == "fail"
    assert "Unsupported sandbox platform: UnknownOS" in result.detail
    assert "bubblewrap" not in result.detail


def test_macos_uses_trusted_sandbox_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
    observed: list[tuple[Path, str]] = []

    def trusted(path: Path, label: str) -> Path:
        observed.append((path, label))
        return path

    monkeypatch.setattr(sandbox, "_trusted_launcher", trusted)
    result = sandbox_result()
    assert result.level == "ok"
    assert observed == [(Path("/usr/bin/sandbox-exec"), "macOS sandbox-exec")]
