"""Exercise the updater across a real interpreter boundary, entirely offline.

Only the package transaction and dependency installers are fixture code. The
updater itself runs unmodified, apart from a release marker used to distinguish
the already-imported copy from the package that replaced it on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import venv

import packaging
import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _metadata(path: Path, name: str, version: str, requires: str = "") -> None:
    _write(path / "METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requires}\n")


@pytest.mark.parametrize("claude_installed", [False, True], ids=["without-claude", "preserve-claude"])
def test_update_prepares_replacement_release_in_isolated_same_interpreter(
    tmp_path: Path, claude_installed: bool,
) -> None:
    environment = tmp_path / "isolated-install"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    location = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    site_packages = Path(location.stdout.strip())
    # Supply the existing test dependency locally; never invoke pip/downloads.
    shutil.copytree(Path(packaging.__file__).parent, site_packages / "packaging", ignore=shutil.ignore_patterns("__pycache__"))

    installed = site_packages / "supervisor"
    replacement = tmp_path / "replacement" / "supervisor"
    updater_source = (SOURCE_ROOT / "supervisor/update_check.py").read_text(encoding="utf-8")
    for package, marker, version in (
        (installed, "old", "0.5.2"),
        (replacement, "replacement", "0.6.0rc1"),
    ):
        _write(package / "__init__.py", f"__version__ = {version!r}\n")
        _write(package / "runtime/__init__.py", "")
        # Keep the actual updater, including its real __main__ preparation path.
        _write(package / "update_check.py", updater_source.replace(
            "from __future__ import annotations\n",
            f"from __future__ import annotations\nFIXTURE_RELEASE = {marker!r}\n", 1,
        ))
        for filename in ("executables.py", "filesystem_safety.py"):
            shutil.copyfile(SOURCE_ROOT / "supervisor" / filename, package / filename)

    _write(installed / "runtime/install.py", """
        def ensure_worker():
            raise AssertionError("the old runtime installer must not be reused")
    """)
    runtime_receipt = tmp_path / "runtime-receipt.json"
    runtime_directory = tmp_path / "replacement-runtime"
    _write(replacement / "runtime/install.py", f"""
        import json
        import sys
        from pathlib import Path
        import supervisor

        def ensure_worker():
            entry = sys.modules["__main__"]
            assert entry.FIXTURE_RELEASE == "replacement"
            result = {{
                "marker": entry.FIXTURE_RELEASE,
                "package_version": supervisor.__version__,
                "interpreter": sys.executable,
                "prefix": sys.prefix,
                "isolated": sys.flags.isolated,
                "module_file": entry.__file__,
                "cwd": str(Path.cwd()),
            }}
            Path({str(runtime_receipt)!r}).write_text(json.dumps(result), encoding="utf-8")
            # Real installers can print progress before the JSON receipt.
            print("fixture runtime preparation completed")
            return Path({str(runtime_directory)!r})
    """)

    claude_checked = tmp_path / "claude-cli-checked"
    _write(replacement / "runtime/claude.py", f"""
        import importlib.metadata as metadata
        from pathlib import Path

        class ClaudeBackend:
            @staticmethod
            def _bundled_cli_path():
                assert metadata.version("claude-agent-sdk") == "0.2.152"
                Path({str(claude_checked)!r}).write_text("checked", encoding="utf-8")
                return Path("fixture-claude")
    """)
    bello_metadata = site_packages / "bello-0.6.0rc1.dist-info"
    _metadata(bello_metadata, "bello", "0.5.2")
    sdk_metadata = site_packages / "claude_agent_sdk-0.2.152.dist-info"
    if claude_installed:
        _metadata(sdk_metadata, "claude-agent-sdk", "0.2.151")

    # A local pip-shaped fixture records exact optional dependency requests. It
    # cannot download anything and rejects arbitrary packages or credentials.
    pip_receipt = tmp_path / "pip-receipt.json"
    _write(site_packages / "pip/__init__.py", "")
    _write(site_packages / "pip/__main__.py", f"""
        import json
        import sys
        from pathlib import Path

        assert sys.argv[1:] == ["install", "claude-agent-sdk==0.2.152"]
        assert sys.flags.isolated == 1
        Path({str(pip_receipt)!r}).write_text(json.dumps({{
            "args": sys.argv[1:], "interpreter": sys.executable,
        }}), encoding="utf-8")
        destination = Path({str(sdk_metadata)!r})
        destination.mkdir(exist_ok=True)
        (destination / "METADATA").write_text(
            "Metadata-Version: 2.1\\nName: claude-agent-sdk\\nVersion: 0.2.152\\n",
            encoding="utf-8",
        )
    """)

    project = tmp_path / "user-project"
    pythonpath = tmp_path / "injected-pythonpath"
    shadows = []
    for root in (project, pythonpath):
        marker = root / "shadow-imported"
        shadows.append(marker)
        _write(root / "supervisor/__init__.py", f"""
            from pathlib import Path
            Path({str(marker)!r}).write_text("untrusted package loaded", encoding="utf-8")
            raise AssertionError("project or PYTHONPATH shadow was imported")
        """)
    auth_file = tmp_path / "runtime-state" / "auth.json"
    auth_bytes = b'{"fixture": "existing credential state must be preserved"}\n'
    auth_file.parent.mkdir()
    auth_file.write_bytes(auth_bytes)

    driver = tmp_path / "drive-update.py"
    _write(driver, f"""
        import importlib.metadata as metadata
        import json
        import shutil
        import subprocess
        import sys
        from pathlib import Path
        from supervisor import update_check
        from supervisor.runtime import install as old_runtime

        assert update_check.FIXTURE_RELEASE == "old"
        assert update_check.__version__ == "0.5.2"
        try:
            old_runtime.ensure_worker()
        except AssertionError:
            pass
        else:
            raise AssertionError("old runtime fixture did not load")
        transactions = []

        def replace_package(command):
            assert command == [sys.executable, "-I", "-m", "pip", "install", "--upgrade", "bello"]
            transactions.append(command)
            shutil.copytree({str(replacement)!r}, {str(installed)!r}, dirs_exist_ok=True)
            Path({str(bello_metadata / 'METADATA')!r}).write_text(
                "Metadata-Version: 2.1\\nName: bello\\nVersion: 0.6.0rc1\\n"
                'Requires-Dist: claude-agent-sdk==0.2.152; extra == "claude"\\n',
                encoding="utf-8",
            )
            # Simulate metadata loss during the package transaction: the
            # original process must remember whether optional support existed.
            sdk_metadata = Path({str(sdk_metadata / 'METADATA')!r})
            if sdk_metadata.exists():
                sdk_metadata.unlink()
            return subprocess.CompletedProcess(command, 0, "", "")

        update_check._run_package_command = replace_package
        prepared = update_check.run_update(update_check.InstallInfo("bello", "0.5.2", "venv"))
        assert len(transactions) == 1
        # These remain cached, so same-process preparation would be wrong.
        assert update_check.FIXTURE_RELEASE == "old"
        assert update_check.__version__ == "0.5.2"
        print(json.dumps({{"version": prepared.version, "directory": prepared.directory}}))
    """)
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(pythonpath)
    child_env["BELLO_RUNTIME_HOME"] = str(auth_file.parent)
    completed = subprocess.run(
        [str(python), "-I", "-B", str(driver)], cwd=project, env=child_env,
        capture_output=True, text=True, check=False, timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"version": "0.6.0rc1", "directory": str(runtime_directory)}
    receipt = json.loads(runtime_receipt.read_text(encoding="utf-8"))
    assert receipt["marker"] == "replacement"
    assert receipt["package_version"] == "0.6.0rc1"
    assert Path(receipt["interpreter"]).absolute() == python.absolute()
    assert Path(receipt["prefix"]).absolute() == environment.absolute()
    assert receipt["isolated"] == 1
    assert Path(receipt["module_file"]).resolve() == (installed / "update_check.py").resolve()
    assert Path(receipt["cwd"]).resolve() == project.resolve()
    assert all(not marker.exists() for marker in shadows)
    assert auth_file.read_bytes() == auth_bytes
    assert claude_checked.exists() is claude_installed
    assert pip_receipt.exists() is claude_installed
    if claude_installed:
        optional = json.loads(pip_receipt.read_text(encoding="utf-8"))
        assert optional["args"] == ["install", "claude-agent-sdk==0.2.152"]
        assert Path(optional["interpreter"]).absolute() == python.absolute()
    else:
        assert not sdk_metadata.exists()
