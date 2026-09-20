from __future__ import annotations

import json
import subprocess

import pytest

from supervisor import update_check


SDK_REQUIREMENT = "claude-agent-sdk==0.2.152"


def test_plain_venv_dependency_targets_current_python(monkeypatch):
    monkeypatch.setattr(update_check, "_running_inside_venv", lambda: True)
    monkeypatch.setattr(update_check, "detect_install_mode", lambda: "venv")
    monkeypatch.setattr(update_check.sys, "executable", "/chosen/venv/bin/python")

    command, env = update_check._dependency_install_command(SDK_REQUIREMENT)

    assert command == ["/chosen/venv/bin/python", "-I", "-m", "pip", "install", SDK_REQUIREMENT]
    assert env is None


@pytest.mark.parametrize("backend", ["pip", "uv"])
def test_pipx_dependency_keeps_custom_home_suffix_and_recorded_backend(monkeypatch, tmp_path, backend):
    pipx_home = tmp_path / "Custom Home"
    prefix = pipx_home / "venvs" / "bello-preview"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text(json.dumps({
        "backend": backend,
        "main_package": {"package": "bello", "suffix": "-preview"},
    }), encoding="utf-8")
    monkeypatch.setattr(update_check.sys, "prefix", str(prefix))
    monkeypatch.setattr(update_check.sys, "base_prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(update_check, "resolve_trusted_executable", lambda *args, **kwargs: "/trusted/pipx")
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "wrong-home"))
    monkeypatch.setenv("PATH", "/preserved/path")

    command, env = update_check._dependency_install_command(SDK_REQUIREMENT)

    assert command == ["/trusted/pipx", "runpip", "bello-preview", "install", SDK_REQUIREMENT]
    assert env["PIPX_HOME"] == str(pipx_home.resolve())
    assert env["PATH"] == "/preserved/path"
    assert update_check.os.environ["PIPX_HOME"] == str(tmp_path / "wrong-home")
    assert "--include-injected" not in command


def test_pipx_dependency_uses_windows_trusted_resolution(monkeypatch, tmp_path):
    prefix = tmp_path / "pipx" / "venvs" / "bello"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(update_check.sys, "prefix", str(prefix))
    monkeypatch.setattr(update_check.sys, "base_prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(update_check.sys, "platform", "win32")
    calls = []

    def resolve(command, **kwargs):
        calls.append((command, kwargs))
        return r"C:\Program Files\pipx.exe"

    monkeypatch.setattr(update_check, "resolve_trusted_executable", resolve)
    command, _ = update_check._dependency_install_command(SDK_REQUIREMENT)

    assert command[0] == r"C:\Program Files\pipx.exe"
    assert calls[0][1]["windows"] is True


@pytest.mark.parametrize("layout", ["not-venvs", "missing-metadata", "noncanonical-name"])
def test_pipx_dependency_refuses_ambiguous_environment(monkeypatch, tmp_path, layout):
    name = "bello_preview" if layout == "noncanonical-name" else "bello"
    parent = "other" if layout == "not-venvs" else "venvs"
    prefix = tmp_path / "pipx" / parent / name
    prefix.mkdir(parents=True)
    if layout != "missing-metadata":
        (prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(update_check.sys, "prefix", str(prefix))
    monkeypatch.setattr(update_check.sys, "base_prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(update_check, "detect_install_mode", lambda: "pipx")

    with pytest.raises(update_check.UpdateCheckError, match="safely"):
        update_check._dependency_install_command(SDK_REQUIREMENT)


def test_pipx_dependency_does_not_fall_back_to_possibly_missing_pip(monkeypatch, tmp_path):
    prefix = tmp_path / "pipx" / "venvs" / "bello"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(update_check.sys, "prefix", str(prefix))
    monkeypatch.setattr(update_check.sys, "base_prefix", str(tmp_path / "base-python"))
    monkeypatch.setattr(update_check, "resolve_trusted_executable", lambda *args, **kwargs: None)

    with pytest.raises(update_check.UpdateCheckError, match="pipx is required"):
        update_check._dependency_install_command(SDK_REQUIREMENT)


def test_system_environment_dependency_install_is_refused(monkeypatch):
    monkeypatch.setattr(update_check, "_running_inside_venv", lambda: False)

    with pytest.raises(update_check.UpdateCheckError, match="outside"):
        update_check._dependency_install_command(SDK_REQUIREMENT)


def test_package_runner_forwards_scoped_pipx_environment(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(update_check.subprocess, "run", run)
    env = {"PIPX_HOME": "/exact/pipx-home", "PATH": "/chosen/bin"}
    command = ["/trusted/pipx", "runpip", "bello-dev", "install", SDK_REQUIREMENT]

    update_check._run_package_command(command, env=env)

    assert calls[0][0] == command
    assert calls[0][1]["env"] == env


def test_package_runner_keeps_existing_inherited_environment_default(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(update_check.subprocess, "run", run)
    update_check._run_package_command(["pipx", "upgrade", "bello"])

    assert "env" not in calls[0]


@pytest.mark.parametrize("backend", ["pip", "uv"])
def test_main_package_update_uses_the_same_custom_pipx_environment(monkeypatch, tmp_path, backend):
    prefix = tmp_path / "Custom Home" / "venvs" / "bello-preview"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text(json.dumps({"backend": backend}), encoding="utf-8")
    monkeypatch.setattr(update_check.sys, "prefix", str(prefix))
    monkeypatch.setattr(update_check.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(update_check, "resolve_trusted_executable", lambda *args, **kwargs: "/trusted/pipx")
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: False)
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "wrong-home"))
    monkeypatch.setattr(update_check, "prepare_runtime", lambda **kwargs: update_check.PreparedRuntime("0.6.1", "/runtime"))
    calls = []
    monkeypatch.setattr(update_check, "_run_package_command", lambda command, **kwargs: calls.append((command, kwargs)))

    update_check.run_update(update_check.InstallInfo("bello", "0.6.0", "pipx"))

    command, kwargs = calls[0]
    assert command == ["/trusted/pipx", "upgrade", "bello-preview"]
    assert kwargs["env"]["PIPX_HOME"] == str(prefix.resolve().parent.parent)
    assert update_check.os.environ["PIPX_HOME"] == str(tmp_path / "wrong-home")
