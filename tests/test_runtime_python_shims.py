"""Preserve the interpreter prefix through Linux sandbox PATH shims."""

import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from supervisor.runtime import sandbox


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell contract")
@pytest.mark.parametrize("name", ["python", "python3"])
def test_linux_python_shim_preserves_exact_path_arguments_stdio_and_exit(
    monkeypatch, tmp_path, name
):
    target = tmp_path / "runtime with 'quotes' $literal" / "python3"
    target.parent.mkdir()
    target.write_text(
        "#!" + sys.executable + "\nimport json,sys\n"
        'print(json.dumps(sys.argv));print("stderr",file=sys.stderr);sys.exit(7)\n'
    )
    target.chmod(0o700)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="linux"))
    sandbox._stage_tool_shims(
        scratch, sandbox._Toolchain(((name, target),), (target.parent.parent,))
    )
    shim = scratch / "bin" / name
    assert not shim.is_symlink()
    assert shim.stat().st_mode & 0o777 == 0o700
    values = ["argument with spaces", "apostrophe'", "$(not-a-command)", ""]
    result = subprocess.run([str(shim), *values], capture_output=True, text=True)
    assert result.returncode == 7
    assert json.loads(result.stdout) == [str(target), *values]
    assert result.stderr == "stderr\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell contract")
def test_linux_python_shim_preserves_relocation_sensitive_executable_name(
    monkeypatch, tmp_path
):
    target = tmp_path / "runtime" / "bin" / "python3"
    target.parent.mkdir(parents=True)
    target.write_text(
        '#!/bin/sh\ncase "$0" in ' + shlex.quote(str(target))
        + ') printf "runtime-ok\\n";; *) exit 86;; esac\n'
    )
    target.chmod(0o700)
    old_alias = tmp_path / "original-alias"
    old_alias.symlink_to(target)
    assert subprocess.run([str(old_alias)], capture_output=True).returncode == 86
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="linux"))
    sandbox._stage_tool_shims(
        scratch, sandbox._Toolchain((("python3", target),), (target.parent.parent,))
    )
    result = subprocess.run(
        [str(scratch / "bin/python3")], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout == "runtime-ok\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symbolic link contract")
@pytest.mark.parametrize(
    ("platform", "name"),
    [("darwin", "python3"), ("win32", "python"), ("linux", "node"), ("linux", "pip3")],
)
def test_other_tool_shims_remain_symlinks(monkeypatch, tmp_path, platform, name):
    target = tmp_path / "target"
    target.write_text("fixture")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform=platform))
    sandbox._stage_tool_shims(
        scratch, sandbox._Toolchain(((name, target),), (tmp_path,))
    )
    assert (scratch / "bin" / name).is_symlink()
    assert (scratch / "bin" / name).resolve() == target
