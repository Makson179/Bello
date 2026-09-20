from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from supervisor import update_check
from supervisor import doctor
from supervisor.doctor import DoctorResult, format_result


@pytest.fixture(autouse=True)
def _doctor_update_check_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(update_check.SKIP_UPDATE_CHECK_ENV, raising=False)


@pytest.fixture(autouse=True)
def _isolated_runtime_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_runtime_dependency_results", lambda: [
        DoctorResult("ok", "Pinned Pi runtime installed"),
    ])


def test_doctor_result_format_is_readable() -> None:
    assert format_result(DoctorResult("ok", "Python 3.11.8")) == "[OK] Python 3.11.8"
    assert format_result(DoctorResult("warn", "Update available: 0.1.1")) == "[WARN] Update available: 0.1.1"
    assert format_result(DoctorResult("fail", "Codex not found on PATH")) == "[FAIL] Codex not found on PATH"


def test_doctor_collects_required_checks_with_update_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    info = update_check.InstallInfo(
        package_name="bello",
        version="0.1.0",
        install_mode="pipx",
    )
    monkeypatch.setattr(doctor, "_doctor_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor,
        "_probe_result",
        lambda args, ok_message, fail_message, timeout=10.0: DoctorResult("ok", ok_message, "ok"),
    )
    monkeypatch.setattr(
        doctor,
        "_schema_generation_result",
        lambda *_args: DoctorResult("ok", "app-server schema generation OK"),
    )
    monkeypatch.setattr(doctor, "_codex_auth_result", lambda *_args: DoctorResult("ok", "Codex auth OK"))
    monkeypatch.setattr(update_check, "read_install_info", lambda: info)
    monkeypatch.setattr(
        update_check,
        "check_for_update",
        lambda install_info: update_check.UpdateStatus(
            update_check.UpdateState.OUTDATED,
            install_info,
            latest_version="0.1.1",
        ),
    )

    messages = [result.message for result in doctor.collect_doctor_results()]

    assert any(message.startswith("Python ") for message in messages)
    assert "Git found: /usr/bin/git" in messages
    assert "Pinned Pi runtime installed" in messages
    assert not any("app-server" in message for message in messages)
    assert "Bello package: bello 0.1.0" in messages
    assert "Bello executable: /usr/bin/bello" in messages
    assert "Bello install mode: pipx" in messages
    assert "Update available: 0.1.1" in messages


def test_probe_result_fails_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2, "", "nope"),
    )

    result = doctor._probe_result(["codex", "--version"], "ok", "codex failed")

    assert result.level == "fail"
    assert result.message == "codex failed"
    assert result.detail == "nope"


def test_doctor_reports_runtime_failure_without_requiring_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    info = update_check.InstallInfo(
        package_name="bello",
        version="0.1.0",
        install_mode="system",
        metadata_available=False,
        warning="package metadata missing",
    )
    monkeypatch.setattr(doctor, "_doctor_executable", lambda name: None)
    monkeypatch.setattr(doctor, "_runtime_dependency_results", lambda: [
        DoctorResult("fail", "Pi runtime dependency check failed", "Run bello runtime install"),
    ])
    monkeypatch.setattr(update_check, "read_install_info", lambda: info)
    monkeypatch.setattr(
        update_check,
        "check_for_update",
        lambda install_info: update_check.UpdateStatus(
            update_check.UpdateState.UNKNOWN,
            install_info,
            warning="package 'bello' was not found on PyPI",
        ),
    )

    results = doctor.collect_doctor_results()
    by_message = {result.message: result for result in results}

    assert by_message["Pi runtime dependency check failed"].level == "fail"
    assert not any("Codex" in message for message in by_message)
    assert by_message["Bello package metadata could not be read"].level == "fail"


def test_doctor_returns_zero_when_update_check_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    info = update_check.InstallInfo(
        package_name="bello",
        version="0.1.0",
        install_mode="pipx",
    )
    monkeypatch.setattr(doctor, "_doctor_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor,
        "_probe_result",
        lambda args, ok_message, fail_message, timeout=10.0: DoctorResult("ok", ok_message, "ok"),
    )
    monkeypatch.setattr(
        doctor,
        "_schema_generation_result",
        lambda *_args: DoctorResult("ok", "app-server schema generation OK"),
    )
    monkeypatch.setattr(doctor, "_codex_auth_result", lambda *_args: DoctorResult("ok", "Codex auth OK"))
    monkeypatch.setattr(update_check, "read_install_info", lambda: info)
    monkeypatch.setattr(
        update_check,
        "check_for_update",
        lambda install_info: update_check.UpdateStatus(
            update_check.UpdateState.UNKNOWN,
            install_info,
            warning="could not reach PyPI",
        ),
    )

    assert doctor.run_doctor() == 0
    output = capsys.readouterr().out
    assert "[WARN] Could not check for Bello updates" in output
    assert "could not reach PyPI" in output


def test_windows_platform_check_reports_native_shell_and_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor.platform, "release", lambda: "11")
    monkeypatch.setattr(doctor.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        doctor.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(build=26100, product_type=1),
        raising=False,
    )
    monkeypatch.setattr(
        doctor,
        "_doctor_executable",
        lambda name: r"C:\Windows\System32\cmd.exe" if name == "cmd" else None,
    )

    results = doctor._platform_results()

    assert results[0] == DoctorResult("ok", "Native Windows detected: 11", "build 10.0.26100")
    assert results[1] == DoctorResult("ok", "Windows architecture: AMD64 (64-bit Python)")
    assert results[2] == DoctorResult("ok", r"Windows shell found: C:\Windows\System32\cmd.exe")


def test_windows_10_is_reported_as_unsupported_with_upgrade_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor.platform, "release", lambda: "10")
    monkeypatch.setattr(doctor.platform, "version", lambda: "10.0.19045")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        doctor.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(build=19045, product_type=1),
        raising=False,
    )
    monkeypatch.setattr(doctor, "_doctor_executable", lambda _name: r"C:\Windows\System32\cmd.exe")

    result = doctor._platform_results()[0]

    assert result.level == "fail"
    assert result.message == "Unsupported native Windows version: 10"
    assert "Windows 11 or Windows Server 2022/2025" in (result.detail or "")
    assert "build 19045" in (result.detail or "")


def test_non_x64_windows_architecture_is_reported_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.platform, "machine", lambda: "ARM64")

    result = doctor._windows_architecture_result()

    assert result.level == "fail"
    assert "ARM64" in result.message
    assert "Windows on ARM" in (result.detail or "")


def test_32_bit_windows_python_is_reported_as_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86")
    monkeypatch.setattr(doctor.struct, "calcsize", lambda _format: 4)

    result = doctor._windows_architecture_result()

    assert result.level == "fail"
    assert "32-bit Python" in result.message
    assert "64-bit Python" in (result.detail or "")


def test_windows_missing_prerequisites_have_actionable_install_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor, "_doctor_executable", lambda name: None)

    shell = doctor._windows_shell_result()

    assert shell.level == "fail"
    assert "PowerShell 7" in (shell.detail or "")
    assert "Git for Windows" in doctor._missing_git_detail()
    assert "codex login" in doctor._missing_codex_detail()


def test_doctor_skip_update_env_avoids_network_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    info = update_check.InstallInfo(package_name="bello", version="0.1.0", install_mode="pipx")
    monkeypatch.setenv(update_check.SKIP_UPDATE_CHECK_ENV, "1")
    monkeypatch.setattr(doctor, "_doctor_executable", lambda name: f"/tools/{name}")
    monkeypatch.setattr(
        doctor,
        "_probe_result",
        lambda args, ok_message, fail_message, timeout=10.0: DoctorResult("ok", ok_message),
    )
    monkeypatch.setattr(doctor, "_schema_generation_result", lambda *_args: DoctorResult("ok", "schema OK"))
    monkeypatch.setattr(doctor, "_codex_auth_result", lambda *_args: DoctorResult("ok", "auth OK"))
    monkeypatch.setattr(update_check, "read_install_info", lambda: info)

    def unexpected_update_check(_info):
        raise AssertionError("doctor must not contact PyPI when update checks are disabled")

    monkeypatch.setattr(update_check, "check_for_update", unexpected_update_check)

    results = doctor.collect_doctor_results()

    skipped = next(result for result in results if result.message == "Bello update check skipped")
    assert skipped.level == "ok"
    assert skipped.detail == "BELLO_SKIP_UPDATE_CHECK=1"


def test_doctor_does_not_start_the_removed_codex_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    info = update_check.InstallInfo(package_name="bello", version="0.1.0", install_mode="pipx")
    captured: list[list[str]] = []
    schema_executables: list[str] = []
    auth_executables: list[str] = []
    codex = r"C:\Users\me\AppData\Roaming\npm\codex.cmd"

    def which(name: str):
        return codex if name == "codex" else f"/tools/{name}"

    def probe(args, ok_message, fail_message, timeout=10.0):
        captured.append(args)
        return DoctorResult("ok", ok_message)

    monkeypatch.setattr(doctor, "_doctor_executable", which)
    monkeypatch.setattr(doctor, "_probe_result", probe)
    monkeypatch.setattr(
        doctor,
        "_schema_generation_result",
        lambda executable: schema_executables.append(executable) or DoctorResult("ok", "schema OK"),
    )
    monkeypatch.setattr(
        doctor,
        "_codex_auth_result",
        lambda executable: auth_executables.append(executable) or DoctorResult("ok", "auth OK"),
    )
    monkeypatch.setattr(update_check, "read_install_info", lambda: info)
    monkeypatch.setattr(
        update_check,
        "check_for_update",
        lambda install_info: update_check.UpdateStatus(update_check.UpdateState.CURRENT, install_info),
    )

    doctor.collect_doctor_results()

    assert captured == []
    assert schema_executables == []
    assert auth_executables == []
