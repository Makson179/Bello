from __future__ import annotations

import os
from pathlib import Path
import shlex

import pytest

from supervisor import immutable_reads as reads
from supervisor.policy import PolicyEngine, command_analysis_from_policy_decision
from supervisor.schemas import PolicyDecisionKind


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Narrow POSIX recognition; Windows keeps existing deny")


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    original, work = tmp_path / "original", tmp_path / "snapshot"
    original.mkdir()
    work.mkdir()
    task = original / "TASK.md"
    task.write_text("task instructions\n")
    # Grammar tests do not depend on a platform providing every hash utility.
    monkeypatch.setattr(reads, "_system_command", lambda raw, names, **kwargs: Path(raw).name in names
                        and (Path(raw).is_absolute() or Path(raw).parent == Path(".")))
    return work, task


def decide(pinned, command, *, wrapped=True):
    work, task = pinned
    command = command.replace("@TASK@", shlex.quote(str(task))).replace("@ORIGINAL@", shlex.quote(str(task.parent)))
    if wrapped:
        command = shlex.join(["/bin/sh", "-c", command])
    policy = PolicyEngine(work, immutable_paths=(task.parent, task), shell_kind="posix")
    result = policy.evaluate({"command": command, "cwd": str(work)})
    return result, command


def test_actual_cleanup_and_hash_read_routes_unchanged_to_runtime(pinned):
    result, command = decide(pinned, "rm -f ./__pycache__/example.pyc && rmdir ./__pycache__ 2>/dev/null || true\n"
        "find . -maxdepth 3 -type f -print | sort\n"
        "nl -ba logscope.py | sed -n '1,220p'\n"
        "printf '\\n--- protected file hashes ---\\n'\n"
        "shasum sample.jsonl @TASK@")
    assert result.kind == PolicyDecisionKind.ROUTE_LLM
    analysis = command_analysis_from_policy_decision(result)
    assert analysis is not None and analysis.command == command
    assert "__bello_verified_pinned_file_read__" not in str(result.model_dump())


@pytest.mark.parametrize("reader", ["cat", "head", "tail", "wc", "shasum", "sha256sum"])
def test_literal_pinned_file_read_is_not_immutable_write(pinned, reader):
    result, _ = decide(pinned, reader + " @TASK@")
    assert result.kind != PolicyDecisionKind.DENY


@pytest.mark.parametrize("command", [
    "rm @TASK@", "shasum @TASK@; rm @TASK@", "rm @TASK@; shasum @TASK@",
    "cat @TASK@ > @TASK@", "cat @TASK@ >> @TASK@", "cat < @TASK@",
    "shasum @TASK@; printf changed > @TASK@", "shasum @TASK@; rm -rf @ORIGINAL@",
    "shasum @ORIGINAL@", "cat @ORIGINAL@/other.txt; shasum @TASK@",
    "cat @TASK@ |& rm @TASK@", "cat @TASK@ &&\nrm @TASK@",
    "rm -f ';' cat @TASK@", "rm -f \\; cat @TASK@", "rm -f '&&' cat @TASK@",
    "rm -f '||' cat @TASK@", "rm -f '|' cat @TASK@", "rm -f \\| cat @TASK@",
    "shasum --check @TASK@", "sha256sum -c @TASK@", "shasum @TASK@ | sh",
    "shasum @TASK@ | /bin/sh", "printf x | cat @TASK@",
    "eval 'cat @TASK@'", "source @TASK@", "cat @TASK@ &", "cat @TASK@ <<EOF\nx\nEOF",
    "cat $(echo @TASK@)", "cat `echo @TASK@`", "(cat @TASK@)", "cat @TASK@\\\n",
    "python -c 'open(\"@TASK@\",\"w\").write(\"changed\")'; shasum @TASK@",
    "sh -c 'rm ../TASK.md'; shasum @TASK@", "cd ..; rm TASK.md; shasum @TASK@",
    "command cd ..; rm TASK.md; shasum @TASK@", "builtin cd ..; rm TASK.md; shasum @TASK@",
    "pushd ..; rm TASK.md; shasum @TASK@", "popd; rm TASK.md; shasum @TASK@",
    "time cd ..; rm TASK.md; shasum @TASK@", "! cd ..; rm TASK.md; shasum @TASK@",
    "if true; then cd ..; fi; rm TASK.md; shasum @TASK@",
    "for x in ..; do cd ..; done; rm TASK.md; shasum @TASK@",
    "while false; do cd ..; done; shasum @TASK@", "PATH=/evil; shasum @TASK@",
    "alias shasum=evil; shasum @TASK@", "autoload shasum; shasum @TASK@",
    "rm 'quoted\n' shasum @TASK@",
    "env sh -c 'rm ../TASK.md'; shasum @TASK@",
    "env /bin/sh -c 'rm ../TASK.md'; shasum @TASK@",
    "env -S \"sh -c 'rm ../TASK.md'\"; shasum @TASK@",
    "nice sh -c 'rm ../TASK.md'; shasum @TASK@",
    "nohup sh -c 'rm ../TASK.md'; shasum @TASK@",
    "timeout 3 sh -c 'rm ../TASK.md'; shasum @TASK@",
    "env python3 change_task.py; shasum @TASK@",
])
def test_write_or_ambiguous_context_retains_immutable_hard_deny(pinned, command):
    result, _ = decide(pinned, command)
    assert result.kind == PolicyDecisionKind.DENY, result
    assert "immutable" in result.reason


def test_only_explicit_pinned_files_qualify(pinned):
    work, task = pinned
    result = reads.literal_pinned_read_tokens("cat " + shlex.quote(str(task)), cwd=work,
                                             immutable_paths=(task.parent,))
    assert result is None


def test_system_reader_does_not_trust_workspace_or_host_path_shims(tmp_path, monkeypatch):
    shim = tmp_path / "shasum"
    shim.write_text("not a system reader\n")
    shim.chmod(0o755)
    monkeypatch.setattr(reads.shutil, "which", lambda command: str(shim))
    assert not reads._system_command("shasum", reads._READERS)
    assert not reads._system_command(str(shim), reads._READERS)


@pytest.mark.parametrize("path_entry", [".", "bin", ""])
def test_unqualified_reader_rejects_cwd_dependent_path(tmp_path, monkeypatch, path_entry):
    monkeypatch.setenv("PATH", path_entry + os.pathsep + "/usr/bin")
    assert not reads._system_command("cat", reads._READERS, workspace=tmp_path)
    assert reads._system_command("/bin/cat", reads._READERS, workspace=tmp_path)


def test_unqualified_reader_rejects_workspace_path_even_without_current_shadow(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + "/usr/bin")
    assert not reads._system_command("cat", reads._READERS, workspace=tmp_path)
    (directory / "cat").write_text("malicious replacement\n")
    (directory / "cat").chmod(0o755)
    assert not reads._system_command("cat", reads._READERS, workspace=tmp_path)
    assert reads._system_command("/bin/cat", reads._READERS, workspace=tmp_path)


def test_unqualified_system_reader_accepts_only_absolute_host_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert reads._system_command("cat", reads._READERS, workspace=tmp_path)
