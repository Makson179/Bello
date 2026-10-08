from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
from zipfile import ZipFile

import pytest

from scripts import verify_release_install as verify


def test_every_smoke_module_is_an_actual_release_module():
    assert all(importlib.util.find_spec(name) is not None for name in verify.PROBE_MODULES)


def wheel(path: Path, *, windows=False, version="0.7.2", remove=None, extra=None, relocated=False):
    tag = "py3-none-win_amd64" if windows else "py3-none-any"
    files = {name: b"resource" for name in verify.REQUIRED}
    files.update({
        "supervisor/__init__.py": b'__version__ = "0.7.2"\n',
        "bello-0.7.2.dist-info/METADATA": f"Metadata-Version: 2.4\nName: Bello\nVersion: {version}\n".encode(),
        "bello-0.7.2.dist-info/WHEEL": f"Wheel-Version: 1.0\nTag: {tag}\n".encode(),
        "bello-0.7.2.dist-info/RECORD": b"record",
    })
    if windows:
        pe = bytearray(80)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 60, 64)
        pe[64:68] = b"PE\0\0"
        struct.pack_into("<H", pe, 68, 0x8664)
        files[verify.WINDOWS_REQUIRED[0]] = bytes(pe)
        files[verify.WINDOWS_REQUIRED[1]] = b"notices"
    if remove:
        files.pop(remove)
    files.update(extra or {})
    with ZipFile(path, "w") as archive:
        for name, value in files.items():
            if relocated and name.startswith("supervisor/"):
                name = "bello-0.7.2.data/purelib/" + name
            archive.writestr(name, value)
    return files


@pytest.mark.parametrize("windows,relocated", [(False, False), (True, False), (True, True)])
def test_wheel_accepts_actual_installed_layouts_and_hashes_all_package_bytes(tmp_path, windows, relocated):
    path = tmp_path / "release.whl"
    files = wheel(path, windows=windows, relocated=relocated)
    result = verify.wheel_payload(path, windows=windows)
    assert result == {name: verify.digest(value) for name, value in files.items()
                      if not name.endswith(".dist-info/RECORD")}


@pytest.mark.parametrize("case", ["version", "missing_prompt", "empty_lock", "missing_helper", "wrong_pe",
                                 "wrong_platform", "traversal", "duplicate_install"])
def test_invalid_wheel_payload_is_rejected(tmp_path, case):
    path = tmp_path / "bad.whl"
    windows = case in {"missing_helper", "wrong_pe"}
    options = {}
    if case == "version":
        options["version"] = "0.7.1"
    if case == "missing_prompt":
        options["remove"] = verify.REQUIRED[0]
    if case == "empty_lock":
        options["extra"] = {"supervisor/pi_worker/package-lock.json": b""}
    if case == "missing_helper":
        options["remove"] = verify.WINDOWS_REQUIRED[0]
    if case == "wrong_pe":
        options["extra"] = {verify.WINDOWS_REQUIRED[0]: b"not an executable"}
    if case == "traversal":
        options["extra"] = {"../outside": b"outside"}
    if case == "duplicate_install":
        options["extra"] = {"bello-0.7.2.data/purelib/supervisor/__init__.py": b"duplicate"}
    wheel(path, windows=windows, **options)
    with pytest.raises(verify.VerificationError):
        verify.wheel_payload(path, windows=not windows if case == "wrong_platform" else windows)


@pytest.mark.parametrize("event", ["socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"])
def test_offline_probe_rejects_network_audit_events(event):
    with pytest.raises(RuntimeError, match="network forbidden"):
        verify.deny_network(event, ())
    verify.deny_network("open", ())


def test_environment_never_forwards_credentials_pythonpath_or_provider_overrides(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "PYTHONPATH", "PYTHONHOME",
                "BELLO_PROMPTS_FILE", "BELLO_CODEX_BINARY", "BELLO_RUNTIME_DIR", "PIP_INDEX_URL"):
        monkeypatch.setenv(key, "synthetic-not-forwarded")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    result = verify.isolated_environment(tmp_path, tmp_path / "venv/Scripts")
    assert "synthetic-not-forwarded" not in result.values()
    assert result["PROCESSOR_ARCHITECTURE"] == "AMD64"
    assert result["BELLO_SKIP_UPDATE_CHECK"] == "1"
    assert result["HOME"] == str(tmp_path / "home")
    assert result["BELLO_NODE"] == str(tmp_path / "home/absent-node")
    assert result["BELLO_CODEX_BINARY"] == str(tmp_path / "home/absent-codex")
    assert all(key not in result for key in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "GH_TOKEN"))


def build_fixture(tmp_path, monkeypatch):
    source, dist, rebuilt = (tmp_path / name for name in ("source", "dist", "rebuilt"))
    for directory in (source, dist, rebuilt):
        directory.mkdir()
    for relative in (*verify.SDIST_REQUIRED, "native/windows-sandbox/src/extra.rs"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
    wheel(dist / "bello-0.7.2-py3-none-any.whl")
    wheel(rebuilt / "bello-0.7.2-py3-none-any.whl")
    with tarfile.open(dist / "bello-0.7.2.tar.gz", "w:gz") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.add(path, arcname="bello-0.7.2/" + path.relative_to(source).as_posix())
    monkeypatch.setattr(verify.subprocess, "check_output", lambda *args, **kwargs: "a" * 40 + "\n")
    return argparse.Namespace(source_root=source, source_sha="a" * 40, dist_dir=dist,
                              rebuilt_dir=rebuilt, windows=False)


def test_build_receipt_binds_exact_wheel_sdist_rebuild_and_native_sources(tmp_path, monkeypatch):
    args = build_fixture(tmp_path, monkeypatch)
    result = verify.build_check(args)
    assert result["status"] == "PASS" and result["source_sha"] == "a" * 40
    assert result["rebuilt_wheel"]["payload_matches"]
    assert set(result["artifacts"]) == {"bello-0.7.2-py3-none-any.whl", "bello-0.7.2.tar.gz"}


@pytest.mark.parametrize("case", ["wrong_head", "changed_rebuild", "changed_native_source", "missing_rebuild"])
def test_build_receipt_rejects_unbound_inputs(tmp_path, monkeypatch, case):
    args = build_fixture(tmp_path, monkeypatch)
    if case == "wrong_head":
        args.source_sha = "b" * 40
    elif case == "changed_rebuild":
        wheel(args.rebuilt_dir / "bello-0.7.2-py3-none-any.whl", extra={"supervisor/__init__.py": b"changed"})
    elif case == "changed_native_source":
        (args.source_root / "native/windows-sandbox/src/extra.rs").write_bytes(b"changed")
    else:
        args.rebuilt_dir = None
    with pytest.raises(verify.VerificationError):
        verify.build_check(args)


def test_clean_install_refuses_work_directory_under_checkout_before_creating_venv(tmp_path, monkeypatch):
    args = argparse.Namespace(source_root=tmp_path, work_dir=tmp_path / "venv")
    with pytest.raises(verify.VerificationError, match="outside source"):
        verify.clean_install(args)
    assert not args.work_dir.exists()


def test_clean_install_binds_receipt_before_install_and_uses_isolated_children(tmp_path, monkeypatch):
    # The children are mocked; native Windows installation is exercised by CI.
    monkeypatch.setattr(verify.sys, "platform", "darwin")
    args = build_fixture(tmp_path, monkeypatch)
    receipt = verify.build_check(args)
    build_report = tmp_path / "build-report.json"
    build_report.write_text(json.dumps(receipt))
    install = argparse.Namespace(source_root=args.source_root, work_dir=tmp_path / "fresh",
                                 build_report=build_report, source_sha=args.source_sha, wheel_dir=args.dist_dir)
    monkeypatch.setattr(verify.venv.EnvBuilder, "create", lambda self, path: None)
    calls = []

    def run(command, label, **kwargs):
        calls.append((command, label, kwargs))
        if label == "installed-probe":
            Path(command[-1]).write_text(json.dumps({"status": "PASS"}))
        return subprocess.CompletedProcess(command, 0, b"Usage: bello\ndoctor\n", b"")

    monkeypatch.setattr(verify, "run_logged", run)
    result = verify.clean_install(install)
    assert result["status"] == "PASS"
    assert [item[1] for item in calls] == ["pip-install", "module-help", "console-help", "installed-probe"]
    assert all(not item[2]["cwd"].is_relative_to(args.source_root) for item in calls)
    assert all(item[2]["environment"]["BELLO_SKIP_UPDATE_CHECK"] == "1" for item in calls)
    assert "-I" in calls[0][0] and "--isolated" in calls[0][0]
    assert "-I" in calls[1][0] and "-I" in calls[3][0]


@pytest.mark.parametrize("case", ["changed_wheel", "wrong_head", "failed_build", "changed_verifier"])
def test_clean_install_refuses_unverified_artifact_before_pip(tmp_path, monkeypatch, case):
    monkeypatch.setattr(verify.sys, "platform", "darwin")
    args = build_fixture(tmp_path, monkeypatch)
    receipt = verify.build_check(args)
    if case == "changed_wheel":
        wheel(args.dist_dir / "bello-0.7.2-py3-none-any.whl", extra={"supervisor/__init__.py": b"changed"})
    elif case == "wrong_head":
        receipt["source_sha"] = "b" * 40
    elif case == "changed_verifier":
        receipt["verifier_sha256"] = "b" * 64
    else:
        receipt["status"] = "FAIL"
    report = tmp_path / "build-report.json"
    report.write_text(json.dumps(receipt))
    install = argparse.Namespace(source_root=args.source_root, work_dir=tmp_path / "fresh",
                                 build_report=report, source_sha=args.source_sha, wheel_dir=args.dist_dir)
    monkeypatch.setattr(verify.venv.EnvBuilder, "create", lambda *_args: pytest.fail("venv created before verification"))
    with pytest.raises(verify.VerificationError):
        verify.clean_install(install)


def test_command_failure_keeps_logs_and_never_becomes_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 7, b"synthetic out", b"synthetic error"))
    with pytest.raises(verify.VerificationError, match="smoke failed"):
        verify.run_logged(["synthetic"], "smoke", cwd=tmp_path, environment={}, output=tmp_path)
    assert (tmp_path / "smoke.stdout.log").read_bytes() == b"synthetic out"
    assert (tmp_path / "smoke.stderr.log").read_bytes() == b"synthetic error"


def test_failure_receipt_excludes_untrusted_exception_text(tmp_path, monkeypatch):
    report = tmp_path / "failure.json"
    monkeypatch.setattr(sys, "argv", ["verify", "build-check", "--source-root", str(tmp_path),
                                     "--source-sha", "a" * 40, "--dist-dir", str(tmp_path),
                                     "--report", str(report)])
    monkeypatch.setattr(verify, "build_check", lambda _args: (_ for _ in ()).throw(RuntimeError("untrusted-private-text")))
    assert verify.main() == 1
    assert "untrusted-private-text" not in report.read_text()
    assert json.loads(report.read_text())["error_type"] == "RuntimeError"
