from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner
import pytest

from supervisor.main import cli
from supervisor.runtime import windows_sandbox as backend


def response(operation: str = "status", *, prepared: bool = True, changed: bool = False) -> dict:
    return {
        "protocolVersion": 1,
        "kind": "hostPreparation",
        "operation": operation,
        "systemRoot": "C:\\",
        "capabilityName": "Bello.Sandbox.SystemRootMetadata.v1",
        "metadataMask": 0x120088,
        "capabilitySid": "S-1-15-3-1024-1",
        "prepared": prepared,
        "changed": changed,
        "targets": [
            {"kind": "systemDriveRoot", "path": "C:\\", "prepared": prepared, "changed": changed},
            {"kind": "userProfiles", "path": "C:\\Users", "prepared": prepared, "changed": changed},
        ],
    }


@pytest.fixture
def host(monkeypatch, tmp_path):
    helper = tmp_path / "trusted" / "bello-windows-sandbox.exe"
    monkeypatch.setattr(backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(backend, "_helper_path", lambda root, mode: helper)
    return helper


@pytest.mark.parametrize("operation", ["status", "prepare", "remove"])
def test_host_preparation_uses_only_fixed_helper_command(monkeypatch, host, operation):
    seen = []
    result = response(operation, prepared=operation != "remove", changed=operation != "status")

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.host_preparation(operation) == result
    command, kwargs = seen.pop()
    assert command == [str(host), f"host-{operation}"]
    assert kwargs["env"] == {}
    assert kwargs["cwd"] == host.parent
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 60
    assert not kwargs.get("shell")


def test_unknown_operation_does_not_start_a_helper(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unexpected process"))
    with pytest.raises(backend.WindowsSandboxBackendError, match="unknown"):
        backend.host_preparation("run")


def test_other_platform_does_not_start_a_helper(monkeypatch):
    monkeypatch.setattr(backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("unexpected process"))
    with pytest.raises(backend.WindowsSandboxUnavailableError, match="only on Windows"):
        backend.host_preparation("prepare")


@pytest.mark.parametrize("patch", [
    {"protocolVersion": True}, {"protocolVersion": 2}, {"kind": "exit"},
    {"operation": "prepare"}, {"prepared": 1}, {"changed": "false"},
    {"changed": True}, {"systemRoot": "C:\\private"}, {"systemRoot": "\\\\host\\share"},
    {"systemRoot": "1:\\"},
    {"metadataMask": 0x1F01FF}, {"metadataMask": True},
    {"capabilityName": "internetClient"}, {"capabilitySid": "S-1-15-2-1"},
    {"targets": []}, {"targets": [False, False]},
])
def test_invalid_status_response_is_rejected(monkeypatch, host, patch):
    value = response() | patch
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, json.dumps(value).encode(), b""
    ))
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend.host_preparation("status")


@pytest.mark.parametrize("patch", [
    {"kind": []}, {"kind": "systemDriveRoot"}, {"path": "C:\\"},
    {"path": "\\\\host\\share"}, {"path": "C:\\Users\\..\\private"},
    {"path": "\\\\?\\C:\\Users"}, {"prepared": 1}, {"changed": None},
    {"prepared": False}, {"changed": True},
])
def test_invalid_target_is_rejected(monkeypatch, host, patch):
    value = response()
    value["targets"][1].update(patch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, json.dumps(value).encode(), b""
    ))
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend.host_preparation("status")


def test_mixed_status_is_valid_but_incomplete_remove_is_rejected(monkeypatch, host):
    value = response(prepared=False)
    value["targets"][0]["prepared"] = True
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, json.dumps(value).encode(), b""
    ))
    assert backend.host_preparation("status") == value
    value["operation"] = "remove"
    with pytest.raises(backend.WindowsSandboxBackendError, match="not removed"):
        backend.host_preparation("remove")


@pytest.mark.parametrize(("operation", "prepared"), [("prepare", False), ("remove", True)])
def test_failed_postcondition_is_not_reported_as_success(monkeypatch, host, operation, prepared):
    value = response(operation, prepared=prepared)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, json.dumps(value).encode(), b""
    ))
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend.host_preparation(operation)


@pytest.mark.parametrize(("stdout", "stderr"), [(b"", b""), (b"not json", b""),
    (b"x" * (backend.MAX_CONTROL_BYTES + 1), b""), (json.dumps(response()).encode(), b"unexpected")])
def test_invalid_helper_output_is_rejected(monkeypatch, host, stdout, stderr):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout, stderr))
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend.host_preparation("status")


def test_native_admin_error_is_preserved(monkeypatch, host):
    error = {"protocolVersion": 1, "kind": "error", "code": "sandbox_backend_error",
             "message": "host-prepare requires an administrator terminal"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 2, b"", json.dumps(error).encode() + b"\n"
    ))
    with pytest.raises(backend.WindowsSandboxBackendError, match="administrator terminal"):
        backend.host_preparation("prepare")


def test_cli_status_does_not_change_permissions(monkeypatch):
    calls = []
    monkeypatch.setattr(backend, "host_preparation", lambda operation: calls.append(operation) or response(prepared=False))
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", "status"])
    assert result.exit_code == 0
    assert calls == ["status"]
    assert "administrator terminal" in result.output
    assert "not every workspace" in result.output


@pytest.mark.parametrize("operation", ["prepare", "remove"])
def test_cli_declining_confirmation_does_not_change_permissions(monkeypatch, operation):
    calls = []
    monkeypatch.setattr(backend, "host_preparation", lambda op: calls.append(op) or response(prepared=operation == "remove"))
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", operation], input="n\n")
    assert result.exit_code != 0
    assert calls == ["status"]


@pytest.mark.parametrize("operation", ["prepare", "remove"])
def test_cli_approved_change_runs_only_requested_setup(monkeypatch, operation):
    calls = []

    def run(op):
        calls.append(op)
        return response(op, prepared=(operation == "remove") if op == "status" else operation == "prepare")

    monkeypatch.setattr(backend, "host_preparation", run)
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", operation, "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == ["status", operation]
    assert "normal" in result.output


@pytest.mark.parametrize("operation", ["prepare", "remove"])
def test_cli_already_in_requested_state_never_mutates(monkeypatch, operation):
    calls = []
    monkeypatch.setattr(backend, "host_preparation", lambda op: calls.append(op) or response(prepared=operation == "prepare"))
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", operation])
    assert result.exit_code == 0
    assert calls == ["status"]


@pytest.mark.parametrize("operation", ["prepare", "remove"])
def test_cli_mixed_setup_runs_requested_operation(monkeypatch, operation):
    calls = []

    def run(op):
        calls.append(op)
        if op == "status":
            value = response(prepared=False)
            value["targets"][0]["prepared"] = True
            return value
        return response(op, prepared=operation == "prepare", changed=True)

    monkeypatch.setattr(backend, "host_preparation", run)
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", operation, "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == ["status", operation]
    assert "C:\\Users" in result.output
