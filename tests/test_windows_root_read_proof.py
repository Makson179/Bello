import importlib.util
import json
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location("root_read_native_proof",
    Path(__file__).resolve().parents[1] / "scripts" / "verify_native_codex_selection.py")
proof = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proof
spec.loader.exec_module(proof)


def fixture(root):
    for name in ("work", "empty-home", "bridge"):
        (root / name).mkdir()
    command = proof.windows_root_read_fixture(root / "work", root / "empty-home")
    observed = {name: False for name in proof.WINDOWS_ROOT_READ_FIELDS}
    (root / "work/windows-root-read-probe.json").write_text(json.dumps(observed))
    return command, observed


def test_new_profile_keeps_root_read_private_denies_and_work_only_writes(tmp_path):
    fixture(tmp_path)
    result = proof.windows_permission_params(tmp_path / "work", tmp_path / "empty-home", tmp_path / "codex.exe")
    config = result["config"]
    fs = config["permissions"]["bello-native"]["filesystem"]
    assert fs[str(tmp_path.anchor)] == "read"
    assert fs[str(tmp_path / "empty-home")] == fs[str(tmp_path / "bridge")] == "deny"
    assert {path for path, mode in fs.items() if mode == "write"} == {
        str(tmp_path / "work"), str(tmp_path / "work/.bello-native-tmp")}
    assert config["shell_environment_policy"]["set"] == {
        name: str(tmp_path / "work/.bello-native-tmp") for name in ("TMP", "TEMP", "TMPDIR")}
    assert config["windows"]["sandbox"] == "elevated"
    assert config["permissions"]["bello-native"]["network"] == {"enabled": False}


def test_canary_command_requires_real_denials_and_preserved_fixtures(tmp_path):
    command, observed = fixture(tmp_path)
    assert command.count("[UnauthorizedAccessException]") == 5
    assert "Write-Output" not in command and "Console]::Out" not in command
    result = proof.windows_root_read_result(tmp_path)
    assert result["windows_private_and_metadata_enforced"] is True
    assert result["windows_private_and_metadata_proof"] == observed


@pytest.mark.parametrize("field", proof.WINDOWS_ROOT_READ_FIELDS)
@pytest.mark.parametrize("value", [True, 0, None, "false"])
def test_any_escape_or_nonboolean_fails(tmp_path, field, value):
    _, observed = fixture(tmp_path)
    observed[field] = value
    (tmp_path / "work/windows-root-read-probe.json").write_text(json.dumps(observed))
    assert proof.windows_root_read_result(tmp_path)["windows_private_and_metadata_enforced"] is False


@pytest.mark.parametrize("path", ["empty-home/root-read-canary.txt", "bridge/root-read-canary.txt",
    "work/.git/root-read-canary.txt", "work/.agents/root-read-canary.txt", "work/.codex/root-read-canary.txt"])
def test_metadata_or_private_fixture_change_fails(tmp_path, path):
    fixture(tmp_path)
    (tmp_path / path).write_text("modified")
    assert proof.windows_root_read_result(tmp_path)["windows_private_and_metadata_enforced"] is False


@pytest.mark.parametrize("payload", ["{}", "[]", "null", "{", "x" * 4097])
def test_missing_or_malformed_observation_fails(tmp_path, payload):
    fixture(tmp_path)
    (tmp_path / "work/windows-root-read-probe.json").write_text(payload)
    assert proof.windows_root_read_result(tmp_path)["windows_private_and_metadata_enforced"] is False
