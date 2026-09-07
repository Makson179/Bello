from __future__ import annotations

import json
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import sys
import tarfile
import zipfile

import pytest

from supervisor.runtime import install as runtime_install


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PINNED_PI_PACKAGE = Path("node_modules/@earendil-works/pi-coding-agent/package.json")


def test_built_wheel_contains_runtime_sources_but_not_node_modules(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path),
            str(_PROJECT_ROOT),
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())

    required = {
        "supervisor/pi_worker/LICENSE-Pi",
        "supervisor/pi_worker/NOTICE.md",
        "supervisor/pi_worker/auth.mjs",
        "supervisor/pi_worker/package-lock.json",
        "supervisor/pi_worker/package.json",
        "supervisor/pi_worker/worker.mjs",
        "supervisor/pi_worker/src/auth-cli.mjs",
        "supervisor/pi_worker/src/pi-sdk.mjs",
        "supervisor/pi_worker/src/protocol.mjs",
        "supervisor/pi_worker/src/runtime.mjs",
        "supervisor/runtime/file_worker.py",
        "supervisor/runtime/tools.json",
    }
    assert required <= names
    assert not any("node_modules" in Path(name).parts for name in names)


def test_built_sdist_contains_native_sources_and_exact_notices_only(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from setuptools.build_meta import build_sdist\n"
                "build_sdist(sys.argv[1])\n"
            ),
            str(tmp_path),
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1

    files: dict[PurePosixPath, bytes] = {}
    with tarfile.open(archives[0], "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or len(path.parts) < 2:
                continue
            relative = PurePosixPath(*path.parts[1:])
            extracted = archive.extractfile(member)
            assert extracted is not None
            files[relative] = extracted.read()

    native_root = PurePosixPath("native/windows-sandbox")
    rust_sources = {
        native_root / "src" / source.name
        for source in (_PROJECT_ROOT / "native/windows-sandbox/src").glob("*.rs")
    }
    required = {
        PurePosixPath("MANIFEST.in"),
        PurePosixPath("setup.py"),
        native_root / "Cargo.lock",
        native_root / "Cargo.toml",
        native_root / "README.md",
        native_root / "ci/native_smoke.py",
        native_root / "ci/run_standard_user.ps1",
        *rust_sources,
    }
    assert rust_sources
    assert required <= files.keys()

    notices = {
        PurePosixPath("LICENSE"),
        native_root / "THIRD_PARTY_NOTICES.txt",
        PurePosixPath("supervisor/pi_worker/LICENSE-Pi"),
        PurePosixPath("supervisor/pi_worker/NOTICE.md"),
    }
    for notice in notices:
        source = _PROJECT_ROOT.joinpath(*notice.parts)
        assert files[notice] == source.read_bytes()

    forbidden_roots = {
        ".bench-tmp",
        ".codex",
        ".supervisor",
        ".test-runtime",
        "Bello-ProgramBench-XHigh-Comparison-Patches",
        "experiments",
        "local-skills",
        "plugins",
        "tmp",
    }
    forbidden_files = {
        PurePosixPath("Bello-ProgramBench-XHigh-Comparison-Patches.zip"),
        PurePosixPath("README_2.md"),
        PurePosixPath("bello-demo.mp4"),
        PurePosixPath("bello_pixel_intro.gif"),
        PurePosixPath("bello_pixel_intro_optimized_small.gif"),
        PurePosixPath("for.md"),
        PurePosixPath("need.md"),
        PurePosixPath("poem.txt"),
        PurePosixPath("programbench_ca_run_info.csv"),
        PurePosixPath("programbench_run_info.csv"),
        PurePosixPath("scripts/plot_programbench_efficient_budget.py"),
    }
    assert not any(
        "node_modules" in path.parts
        or "target" in path.parts
        or path.parts[0] in forbidden_roots
        or path in forbidden_files
        or path.name == ".DS_Store"
        or path.suffix in {".log", ".pyc"}
        for path in files
    )


def test_node_executable_rejects_a_runtime_below_pi_minimum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = tmp_path / "node"
    node.touch()
    monkeypatch.setenv("BELLO_NODE", str(node))
    monkeypatch.setattr(runtime_install, "require_trusted_executable", lambda *_args, **_kwargs: str(node))
    monkeypatch.setattr(
        runtime_install.subprocess,
        "run",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args[0], 0, "v22.18.0\n", ""),
    )

    with pytest.raises(RuntimeError, match=r"Pi requires Node\.js >= 22\.19\.0; found 22\.18\.0"):
        runtime_install.node_executable()


def test_install_worker_rejects_a_distribution_without_the_pinned_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.mjs").write_text("// fixture\n", encoding="utf-8")
    monkeypatch.setattr(runtime_install, "node_executable", lambda: str(tmp_path / "node"))
    monkeypatch.setattr(runtime_install, "source_worker_directory", lambda: source)

    with pytest.raises(RuntimeError, match="pinned Pi dependency lockfile is missing"):
        runtime_install.install_worker()


def test_install_worker_rejects_an_installed_pi_version_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "installed"
    source.mkdir()
    destination.mkdir()
    (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "worker.mjs").write_text("// fixture\n", encoding="utf-8")
    installed_manifest = destination / _PINNED_PI_PACKAGE
    installed_manifest.parent.mkdir(parents=True)
    installed_manifest.write_text(json.dumps({"version": "0.85.0"}), encoding="utf-8")

    node = tmp_path / "trusted" / "node"
    npm = tmp_path / "trusted" / "npm"
    calls: list[tuple[list[str], Path]] = []

    def fake_run(args: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> subprocess.CompletedProcess:
        assert check is True
        assert str(node.parent) == env["PATH"].split(runtime_install.os.pathsep, 1)[0]
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(runtime_install, "node_executable", lambda: str(node))
    monkeypatch.setattr(runtime_install, "source_worker_directory", lambda: source)
    monkeypatch.setattr(runtime_install, "worker_directory", lambda: destination)
    monkeypatch.setattr(runtime_install, "require_trusted_executable", lambda *_args, **_kwargs: str(npm))
    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Pi installation version does not match Bello's pinned runtime"):
        runtime_install.install_worker()

    assert calls == [
        ([str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"], destination),
    ]
