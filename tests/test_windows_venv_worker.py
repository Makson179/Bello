"""Issue #41: a Windows venv worker must be the process the guardian started.

A Windows venv's python.exe is a redirector that runs the base interpreter as a
child, so the worker's reported PID differed from the guardian's Popen PID and
every pipx run failed with "process guardian rejected the worker".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from supervisor.process_fence import _windows_direct_interpreter

COMMAND_TAIL = ["-m", "supervisor.main", "--task", "TASK.md"]


def _fake_interpreters(tmp_path):
    base = tmp_path / "base" / "python.exe"
    redirector = tmp_path / "venv" / "Scripts" / "python.exe"
    for path in (base, redirector):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture")
    return str(base), str(redirector)


def test_windows_venv_worker_starts_the_base_interpreter_with_the_venv_identity(tmp_path):
    base, redirector = _fake_interpreters(tmp_path)
    env = {"KEEP": "1"}
    command, worker_env = _windows_direct_interpreter(
        [redirector, *COMMAND_TAIL], env, windows=True, executable=redirector, base_executable=base)
    assert command == [base, *COMMAND_TAIL]
    assert worker_env == {"KEEP": "1", "__PYVENV_LAUNCHER__": redirector}
    assert env == {"KEEP": "1"}


@pytest.mark.parametrize("case", ["posix", "not_a_venv", "other_program", "missing_base", "empty"])
def test_other_launches_are_unchanged(tmp_path, case):
    base, redirector = _fake_interpreters(tmp_path)
    command = [] if case == "empty" else [redirector, *COMMAND_TAIL]
    options = {"windows": case != "posix", "executable": redirector, "base_executable": base}
    if case == "not_a_venv":
        options["base_executable"] = redirector
    elif case == "other_program":
        command = [str(tmp_path / "other.exe"), *COMMAND_TAIL]
    elif case == "missing_base":
        options["base_executable"] = str(tmp_path / "absent" / "python.exe")
    env = {"KEEP": "1"}
    assert _windows_direct_interpreter(command, env, **options) == (command, env)


@pytest.mark.skipif(os.name != "nt", reason="Windows venv redirector semantics")
def test_real_windows_venv_worker_is_one_process_with_the_venv_identity(tmp_path):
    root = tmp_path / "worker-venv"
    venv.EnvBuilder(with_pip=False).create(root)
    redirector = str(root / "Scripts" / "python.exe")
    base = getattr(sys, "_base_executable", None) or sys.executable
    probe = ("import json, os, sys; print(json.dumps({'pid': os.getpid(), 'executable': sys.executable, "
             "'prefix': sys.prefix, 'launcher': '__PYVENV_LAUNCHER__' in os.environ}))")
    command, env = _windows_direct_interpreter(
        [redirector, "-c", probe], dict(os.environ), executable=redirector, base_executable=base)
    assert command[0] == base and env["__PYVENV_LAUNCHER__"] == redirector
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
    output, _ = process.communicate(timeout=60)
    assert process.returncode == 0
    reported = json.loads(output)
    assert reported["pid"] == process.pid
    assert os.path.normcase(reported["executable"]) == os.path.normcase(redirector)
    assert os.path.normcase(str(Path(reported["prefix"]).resolve())) == os.path.normcase(str(root.resolve()))
    assert reported["launcher"] is False
