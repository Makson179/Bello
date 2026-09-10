from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from supervisor import update_check
from supervisor.runtime import install
from supervisor.runtime.claude import ClaudeBackend


def _info():
    return update_check.InstallInfo("bello", "0.6.0", "venv")


@pytest.mark.parametrize("with_claude", [False, True])
def test_update_prepares_only_after_package_success(monkeypatch, with_claude):
    calls = []
    expected = update_check.PreparedRuntime("0.6.1", "/new/runtime")
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: with_claude)
    monkeypatch.setattr(update_check, "update_command", lambda info: ["package-manager"])
    monkeypatch.setattr(update_check, "_run_package_command", lambda cmd: calls.append(cmd))
    def prepare(**kwargs):
        calls.append(kwargs)
        return expected
    monkeypatch.setattr(update_check, "prepare_runtime", prepare)
    assert update_check.run_update(_info()) == expected
    assert calls == [["package-manager"], {"with_claude": with_claude}]


def test_package_failure_does_not_prepare_or_claim_partial_success(monkeypatch):
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: False)
    monkeypatch.setattr(update_check, "update_command", lambda info: ["package-manager"])
    def fail(cmd):
        raise update_check.UpdateCheckError("package failed")
    monkeypatch.setattr(update_check, "_run_package_command", fail)
    monkeypatch.setattr(update_check, "prepare_runtime", lambda **kwargs: pytest.fail("must not prepare"))
    with pytest.raises(update_check.UpdateCheckError, match="^package failed$"):
        update_check.run_update(_info())


def test_runtime_failure_reports_partial_update_and_retry(monkeypatch):
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: False)
    monkeypatch.setattr(update_check, "update_command", lambda info: ["package-manager"])
    monkeypatch.setattr(update_check, "_run_package_command", lambda cmd: None)
    def fail(**kwargs):
        raise update_check.UpdateCheckError("npm offline")
    monkeypatch.setattr(update_check, "prepare_runtime", fail)
    with pytest.raises(update_check.UpdateCheckError) as exc:
        update_check.run_update(_info())
    assert "package update finished" in str(exc.value)
    assert "bello update` again" in str(exc.value)
    assert "npm offline" in str(exc.value)


def test_unchanged_package_does_not_report_success_or_allow_reexec_loop(monkeypatch):
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: False)
    monkeypatch.setattr(update_check, "update_command", lambda info: ["package-manager"])
    monkeypatch.setattr(update_check, "_run_package_command", lambda cmd: None)
    monkeypatch.setattr(update_check, "prepare_runtime", lambda **kwargs: update_check.PreparedRuntime("0.6.0", "/ready"))
    with pytest.raises(update_check.UpdateCheckError, match="package source or version pin"):
        update_check.run_update(_info())


@pytest.mark.parametrize("with_claude", [False, True])
def test_prepare_uses_same_isolated_interpreter_and_final_receipt(monkeypatch, with_claude):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "npm progress\n" + json.dumps({
            "version": "0.6.1", "runtime_directory": "/new/runtime",
        }) + "\n", "")
    monkeypatch.setattr(update_check.subprocess, "run", run)
    assert update_check.prepare_runtime(with_claude=with_claude) == update_check.PreparedRuntime("0.6.1", "/new/runtime")
    command, kwargs = calls[0]
    assert command == [update_check.sys.executable, "-I", "-m", "supervisor.update_check", "--prepare-runtime"] + (["--with-claude"] if with_claude else [])
    assert kwargs == {"capture_output": True, "text": True, "check": False}


@pytest.mark.parametrize("stdout", ["", "not json", "{}", "[]", '{"version": "bad", "runtime_directory": "/runtime"}'])
def test_prepare_rejects_missing_or_malformed_receipt(monkeypatch, stdout):
    monkeypatch.setattr(update_check.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout, ""))
    with pytest.raises(update_check.UpdateCheckError, match="valid result"):
        update_check.prepare_runtime(with_claude=False)


def test_prepare_reports_failed_child(monkeypatch):
    monkeypatch.setattr(update_check.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "npm output\n", "missing Node"))
    with pytest.raises(update_check.UpdateCheckError, match="missing Node"):
        update_check.prepare_runtime(with_claude=False)


def test_prepare_reports_unlaunchable_interpreter(monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("missing interpreter")
    monkeypatch.setattr(update_check.subprocess, "run", fail)
    with pytest.raises(update_check.UpdateCheckError, match="missing interpreter"):
        update_check.prepare_runtime(with_claude=False)


@pytest.mark.parametrize("requested,present,expected", [(False, False, []), (False, True, ["claude"]), (True, False, ["claude"])])
def test_fresh_preparation_only_keeps_existing_or_requested_claude(monkeypatch, requested, present, expected):
    calls = []
    monkeypatch.setattr(update_check, "_claude_is_installed", lambda: present)
    monkeypatch.setattr(update_check, "_ensure_claude_dependency", lambda: calls.append("claude"))
    monkeypatch.setattr(install, "ensure_worker", lambda: calls.append("pi") or Path("runtime-new"))
    monkeypatch.setattr(update_check.metadata, "version", lambda name: "0.6.2")
    prepared = update_check._prepare_installed_runtime(with_claude=requested)
    assert calls == expected + ["pi"]
    assert prepared == update_check.PreparedRuntime("0.6.2", "runtime-new")


def _claude_metadata(monkeypatch, version):
    dist = SimpleNamespace(requires=[
        'click>=8.1',
        'claude-agent-sdk==0.9.123; extra == "claude"',
        'claude-agent-sdk==0.1.0; extra == "test"',
    ])
    monkeypatch.setattr(update_check.metadata, "distribution", lambda name: dist)
    def installed(name):
        if version[0] is None:
            raise update_check.metadata.PackageNotFoundError(name)
        return version[0]
    monkeypatch.setattr(update_check.metadata, "version", installed)
    monkeypatch.setattr(update_check, "_running_inside_venv", lambda: True)
    monkeypatch.setattr(update_check, "detect_install_mode", lambda: "venv")
    monkeypatch.setattr(ClaudeBackend, "_bundled_cli_path", lambda: Path("bundled-claude"))


@pytest.mark.parametrize("previous", [None, "0.2.152"])
def test_claude_sync_reads_new_extra_pin_not_old_constant(monkeypatch, previous):
    version = [previous]
    _claude_metadata(monkeypatch, version)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        version[0] = "0.9.123"
    monkeypatch.setattr(update_check, "_run_package_command", run)
    update_check._ensure_claude_dependency()
    assert len(commands) == 1
    assert commands[0][-1] == "claude-agent-sdk==0.9.123"
    assert "--include-injected" not in commands[0]


def test_matching_claude_is_reused_without_install(monkeypatch):
    _claude_metadata(monkeypatch, ["0.9.123"])
    monkeypatch.setattr(update_check, "_run_package_command", lambda *args, **kwargs: pytest.fail("already compatible"))
    update_check._ensure_claude_dependency()


def test_claude_sync_rejects_installer_success_without_correct_version(monkeypatch):
    _claude_metadata(monkeypatch, ["0.2.152"])
    monkeypatch.setattr(update_check, "_run_package_command", lambda *args, **kwargs: None)
    with pytest.raises(update_check.UpdateCheckError, match="does not match"):
        update_check._ensure_claude_dependency()


@pytest.mark.parametrize("requirements", [[], ['claude-agent-sdk; extra == "claude"'], ['claude-agent-sdk==0.9; extra == "test"']])
def test_claude_does_not_guess_dependency_from_incomplete_metadata(monkeypatch, requirements):
    monkeypatch.setattr(update_check.metadata, "distribution", lambda name: SimpleNamespace(requires=requirements))
    monkeypatch.setattr(update_check, "_run_package_command", lambda *args, **kwargs: pytest.fail("must not guess"))
    with pytest.raises(update_check.UpdateCheckError, match="metadata does not specify"):
        update_check._ensure_claude_dependency()


@pytest.mark.parametrize("failure", [OSError("cannot launch"), subprocess.TimeoutExpired(["pip"], 300)])
def test_package_process_errors_are_actionable(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(update_check.subprocess, "run", fail)
    with pytest.raises(update_check.UpdateCheckError):
        update_check._run_package_command(["pip"])
