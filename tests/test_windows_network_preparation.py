from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from supervisor.main import cli
from supervisor.runtime import windows_sandbox as backend


def response(operation="status", *, installed=True, changed=False):
    return {
        "protocolVersion": 1, "policyVersion": 1, "kind": "networkPreparation",
        "operation": operation, "serviceName": "BelloOfflineNetwork",
        "installPath": r"C:\Program Files\BelloOfflineNetwork\bello-windows-sandbox.exe",
        "installed": installed, "running": installed, "prepared": installed,
        "binaryMatches": installed, "changed": changed, "quiesced": False,
        "servicePid": 400 if installed else 0, "activeLeases": 0, "retainedLeases": 0,
    }


@pytest.mark.parametrize("operation", ["status", "prepare", "remove"])
def test_network_selector_runs_only_the_fixed_command(monkeypatch, tmp_path, operation):
    helper = tmp_path / "trusted" / "bello-windows-sandbox.exe"
    monkeypatch.setattr(backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(backend, "_helper_path", lambda *a: helper)
    expected = response(operation, installed=operation != "remove", changed=operation != "status")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(expected).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.host_preparation(operation, network=True) == expected
    command, kwargs = seen.pop()
    assert command == [str(helper), f"host-{operation}", "--network"]
    assert kwargs["env"] == {} and kwargs["stdin"] == subprocess.DEVNULL
    assert not kwargs.get("shell")


@pytest.mark.parametrize("kwargs", [
    {"network": 1}, {"network": "true"}, {"network": True, "null_device": True},
    {"network": True, "drive": "D:"},
])
def test_conflicting_network_selector_never_starts_process(monkeypatch, kwargs):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(backend.WindowsSandboxBackendError, match="select only one"):
        backend.host_preparation("prepare", **kwargs)


@pytest.mark.parametrize("patch", [
    {"protocolVersion": True}, {"policyVersion": 2}, {"kind": "hostPreparation"},
    {"serviceName": "OtherService"}, {"operation": "prepare"}, {"changed": True},
    {"installed": False}, {"running": False}, {"prepared": False},
    {"servicePid": True}, {"servicePid": 0}, {"activeLeases": -1}, {"retainedLeases": "0"},
    {"binaryMatches": 1}, {"quiesced": 1}, {"quiesced": True}, {"arbitraryCommand": "anything"},
    {"installPath": r"\\host\share\BelloOfflineNetwork\bello-windows-sandbox.exe"},
    {"installPath": r"C:\Program Files\..\BelloOfflineNetwork\bello-windows-sandbox.exe"},
    {"installPath": r"C:\Program Files\OtherService\bello-windows-sandbox.exe"},
])
def test_network_status_rejects_malformed_or_conflicting_state(patch):
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend._validate_network_preparation(response() | patch, "status")


def test_network_status_missing_service_and_stale_binary_are_valid():
    assert not backend._validate_network_preparation(response(installed=False), "status")["installed"]
    stale = response() | {"binaryMatches": False, "prepared": False}
    assert not backend._validate_network_preparation(stale, "status")["prepared"]
    with pytest.raises(backend.WindowsSandboxBackendError):
        backend._validate_network_preparation(stale | {"operation": "prepare"}, "prepare")


def test_quiesced_service_is_not_ready_and_must_have_no_leases():
    value = response() | {"quiesced": True, "prepared": False}
    assert not backend._validate_network_preparation(value, "status")["prepared"]
    for patch in ({"activeLeases": 1}, {"retainedLeases": 1}, {"running": False, "servicePid": 0}):
        with pytest.raises(backend.WindowsSandboxBackendError, match="inconsistent"):
            backend._validate_network_preparation(value | patch, "status")
    with pytest.raises(backend.WindowsSandboxBackendError, match="inconsistent"):
        backend._validate_network_preparation(value | {"operation": "prepare"}, "prepare")


def test_network_prepare_recovers_quiesced_service_instead_of_returning_ready(monkeypatch):
    seen = []

    def prepare(operation, **kwargs):
        seen.append(operation)
        if operation == "status":
            return response() | {"quiesced": True, "prepared": False}
        return response("prepare", changed=True)

    monkeypatch.setattr(backend, "host_preparation", prepare)
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", "prepare", "--network", "--yes"])
    assert result.exit_code == 0, result.output
    assert seen == ["status", "prepare"]


def test_network_cli_status_is_read_only(monkeypatch):
    seen = []
    monkeypatch.setattr(backend, "host_preparation", lambda op, **kw: seen.append((op, kw)) or response(installed=False))
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", "status", "--network"])
    assert result.exit_code == 0, result.output
    assert seen == [("status", {"drive": None, "null_device": False, "network": True})]
    assert "prepare --network" in result.output


def test_network_prepare_describes_privilege_and_requires_confirmation(monkeypatch):
    seen = []

    def prepare(operation, **kwargs):
        seen.append((operation, kwargs))
        return response(operation, installed=operation != "status", changed=operation == "prepare")

    monkeypatch.setattr(backend, "host_preparation", prepare)
    runner = CliRunner()
    rejected = runner.invoke(cli, ["runtime", "windows-sandbox", "prepare", "--network"], input="n\n")
    assert rejected.exit_code != 0
    assert [op for op, _ in seen] == ["status"]
    assert "LocalSystem" in rejected.output and "without administrator rights" in rejected.output
    seen.clear()
    accepted = runner.invoke(cli, ["runtime", "windows-sandbox", "prepare", "--network", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert seen == [("status", {"network": True}), ("prepare", {"network": True})]


@pytest.mark.parametrize("field", ["activeLeases", "retainedLeases"])
def test_network_remove_refuses_outstanding_leases(monkeypatch, field):
    seen = []
    monkeypatch.setattr(backend, "host_preparation", lambda op, **kw: seen.append(op) or response() | {field: 1})
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", "remove", "--network", "--yes"])
    assert result.exit_code != 0
    assert seen == ["status"]


@pytest.mark.parametrize("extra", [["--null-device"], ["--drive", "D:"]])
def test_network_cli_rejects_multiple_selectors(monkeypatch, extra):
    monkeypatch.setattr(backend, "host_preparation", lambda *a, **kw: pytest.fail("unexpected helper"))
    result = CliRunner().invoke(cli, ["runtime", "windows-sandbox", "prepare", "--network", *extra])
    assert result.exit_code != 0 and "Select only one" in result.output
