from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import sys
import tarfile
from threading import Event
import zipfile

import pytest

from supervisor.runtime import install as runtime_install


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PINNED_PI_PACKAGE = Path("node_modules/@earendil-works/pi-coding-agent/package.json")


def _installed_wheel_files(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """Resolve wheel library relocation paths, not arbitrary archive prefixes."""

    names = [name for name in archive.namelist() if not name.endswith("/")]
    metadata = [name for name in names if name.endswith(".dist-info/WHEEL")]
    assert len(metadata) == 1
    data_root = metadata[0].removesuffix(".dist-info/WHEEL") + ".data/"
    library_roots = (data_root + "purelib/", data_root + "platlib/")
    files: dict[str, bytes] = {}
    for name in names:
        installed = next((name[len(root):] for root in library_roots if name.startswith(root)), name)
        assert installed not in files, f"duplicate installed wheel path: {installed}"
        files[installed] = archive.read(name)
    return files


@pytest.mark.parametrize("library", ["", "purelib", "platlib"])
def test_wheel_file_inspection_resolves_only_library_relocation(library: str) -> None:
    buffer = io.BytesIO()
    package = "bello-0.6.0.dev0"
    relative = "supervisor/runtime/tools.json"
    prefix = f"{package}.data/{library}/" if library else ""
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{package}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(prefix + relative, b'{"tool": "original bytes"}\n')
        archive.writestr(f"{package}.data/data/{relative}", b"not a library location")
        archive.writestr(f"unrelated.data/purelib/{relative}", b"not this distribution")
    with zipfile.ZipFile(buffer) as archive:
        files = _installed_wheel_files(archive)
    assert files[relative] == b'{"tool": "original bytes"}\n'
    assert files[f"{package}.data/data/{relative}"] == b"not a library location"
    assert files[f"unrelated.data/purelib/{relative}"] == b"not this distribution"


def test_wheel_file_inspection_rejects_conflicting_installed_paths() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bello-0.6.0.dev0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr("supervisor/runtime/tools.json", b"root")
        archive.writestr("bello-0.6.0.dev0.data/purelib/supervisor/runtime/tools.json", b"relocated")
    with zipfile.ZipFile(buffer) as archive:
        with pytest.raises(AssertionError, match="duplicate installed wheel path"):
            _installed_wheel_files(archive)


def test_setuptools_platform_wheel_preserves_relocated_package_bytes(tmp_path: Path) -> None:
    # Match Bello's platform-tagged wheel without compiling a Windows binary:
    # root_is_pure=False does not itself change Distribution.has_ext_modules().
    package = tmp_path / "supervisor" / "runtime"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    expected = b'{"fixture": "exact package data"}\n'
    (package / "tools.json").write_bytes(expected)
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "from setuptools.command.bdist_wheel import bdist_wheel\n"
        "class PlatformWheel(bdist_wheel):\n"
        "    def finalize_options(self):\n"
        "        super().finalize_options()\n"
        "        self.root_is_pure = False\n"
        "    def get_tag(self):\n"
        "        return ('py3', 'none', 'win_amd64')\n"
        "setup(name='bello-wheel-fixture', version='1.0',\n"
        "      packages=['supervisor', 'supervisor.runtime'],\n"
        "      package_data={'supervisor.runtime': ['tools.json']},\n"
        "      cmdclass={'bdist_wheel': PlatformWheel})\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "setup.py", "bdist_wheel"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((tmp_path / "dist").glob("*-py3-none-win_amd64.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "bello_wheel_fixture-1.0.data/purelib/supervisor/runtime/tools.json" in archive.namelist()
        files = _installed_wheel_files(archive)
    assert files["supervisor/runtime/tools.json"] == expected


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
        files = _installed_wheel_files(archive)

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
    assert required <= files.keys()
    for relative in required:
        assert files[relative] == (_PROJECT_ROOT / relative).read_bytes()
    assert not any("node_modules" in PurePosixPath(name).parts for name in names)


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


def _ensure_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str, str]:
    source = tmp_path / "source"
    destination = tmp_path / "runtime" / "current-fingerprint"
    source.mkdir()
    (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "package.json").write_text("{}\n", encoding="utf-8")
    (source / "worker.mjs").write_text("// fixture\n", encoding="utf-8")
    node = str(tmp_path / "trusted" / "node")
    npm = str(tmp_path / "trusted" / "npm")
    monkeypatch.setattr(runtime_install, "node_executable", lambda: node)
    monkeypatch.setattr(runtime_install, "source_worker_directory", lambda: source)
    monkeypatch.setattr(runtime_install, "worker_directory", lambda: destination)
    monkeypatch.setattr(runtime_install, "require_trusted_executable", lambda *_args, **_kwargs: npm)
    return source, destination, node, npm


def _ready_fixture(destination: Path, *, version: str = runtime_install.PINNED_PI_VERSION) -> None:
    manifest = destination / _PINNED_PI_PACKAGE
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
    (destination / "worker.mjs").write_text("// installed fixture\n", encoding="utf-8")


@pytest.mark.parametrize("development_install", [False, True])
def test_ensure_worker_reuses_ready_runtime_without_writing_files_or_running_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, development_install: bool,
) -> None:
    source, destination, node, _ = _ensure_fixture(tmp_path, monkeypatch)
    if development_install:
        destination = source
        monkeypatch.setattr(runtime_install, "worker_directory", lambda: source)
    _ready_fixture(destination)
    before = {path.relative_to(destination): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in destination.rglob("*") if path.is_file()}
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)
    assert runtime_install.ensure_worker() == destination
    assert runtime_install.ensure_worker() == destination
    assert len(calls) == 2
    assert all(args == [node, str(destination / "worker.mjs")] for args, _ in calls)
    assert all(kwargs["input"] == "" and kwargs["check"] is False for _, kwargs in calls)
    after = {path.relative_to(destination): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in destination.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("incomplete", ["absent", "missing-entry", "wrong-version", "broken-import"])
def test_ensure_worker_prepares_missing_or_incomplete_pinned_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incomplete: str,
) -> None:
    source, destination, node, npm = _ensure_fixture(tmp_path, monkeypatch)
    if incomplete != "absent":
        _ready_fixture(destination, version="0.85.0" if incomplete == "wrong-version" else runtime_install.PINNED_PI_VERSION)
    if incomplete == "missing-entry":
        (destination / "worker.mjs").unlink()
    old = destination.parent / "old-fingerprint" / "node_modules" / "active-module.js"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("// used by an active old release\n", encoding="utf-8")
    old_stat = old.stat().st_mtime_ns
    installed = False
    calls = []

    def fake_run(args, **kwargs):
        nonlocal installed
        calls.append(args)
        if args[0] == npm:
            assert args == [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
            assert kwargs["cwd"] == destination
            assert kwargs["check"] is True
            assert kwargs["env"]["PATH"].split(runtime_install.os.pathsep)[0] == str(Path(node).parent)
            assert (destination / "package-lock.json").read_bytes() == (source / "package-lock.json").read_bytes()
            _ready_fixture(destination)
            installed = True
        elif incomplete == "broken-import" and not installed:
            return subprocess.CompletedProcess(args, 1, "", "module missing")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)
    assert runtime_install.ensure_worker() == destination
    assert installed
    assert sum(args[0] == npm for args in calls) == 1
    assert calls[-1] == [node, str(destination / "worker.mjs")]
    assert old.read_text(encoding="utf-8") == "// used by an active old release\n"
    assert old.stat().st_mtime_ns == old_stat


def test_ensure_worker_does_not_reinstall_on_readiness_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, destination, node, _ = _ensure_fixture(tmp_path, monkeypatch)
    _ready_fixture(destination)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="readiness check timed out; its files were left unchanged"):
        runtime_install.ensure_worker()
    assert calls == [[node, str(destination / "worker.mjs")]]


def test_ensure_worker_reports_failed_dependency_install_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, destination, _, npm = _ensure_fixture(tmp_path, monkeypatch)

    def failing_run(args, **_kwargs):
        assert args[0] == npm
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(runtime_install.subprocess, "run", failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        runtime_install.ensure_worker()

    def working_run(args, **_kwargs):
        if args[0] == npm:
            _ready_fixture(destination)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_install.subprocess, "run", working_run)
    assert runtime_install.ensure_worker() == destination


def test_ensure_worker_rejects_runtime_that_still_cannot_start_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, destination, _, npm = _ensure_fixture(tmp_path, monkeypatch)

    def fake_run(args, **_kwargs):
        if args[0] == npm:
            _ready_fixture(destination)
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 1, "", "module missing")

    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Pi runtime is not ready after installation"):
        runtime_install.ensure_worker()


def test_ensure_worker_serializes_concurrent_updates_and_installs_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, destination, _, npm = _ensure_fixture(tmp_path, monkeypatch)
    installing = Event()
    second_started = Event()
    release = Event()
    installs = []

    def fake_run(args, **_kwargs):
        if args[0] == npm:
            installs.append(args)
            installing.set()
            assert release.wait(5)
            _ready_fixture(destination)
        return subprocess.CompletedProcess(args, 0, "", "")

    def second_update():
        second_started.set()
        return runtime_install.ensure_worker()

    monkeypatch.setattr(runtime_install.subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runtime_install.ensure_worker)
        assert installing.wait(5)
        second = executor.submit(second_update)
        assert second_started.wait(5)
        release.set()
        assert first.result(timeout=5) == destination
        assert second.result(timeout=5) == destination
    assert len(installs) == 1


@pytest.mark.parametrize("target", ["directory", "lock"])
def test_ensure_worker_rejects_symlink_install_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    _, destination, _, _ = _ensure_fixture(tmp_path, monkeypatch)
    destination.parent.mkdir(parents=True)
    link = destination if target == "directory" else destination.parent / f".{destination.name}.install.lock"
    link_target = tmp_path / "unrelated"
    if target == "directory":
        link_target.mkdir()
    else:
        link_target.touch()
    try:
        link.symlink_to(link_target, target_is_directory=target == "directory")
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows user")
    monkeypatch.setattr(runtime_install.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run npm or Node"))
    with pytest.raises(RuntimeError, match="must not be a symbolic link"):
        runtime_install.ensure_worker()
