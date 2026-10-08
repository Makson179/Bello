#!/usr/bin/env python3
"""Verify built release payloads and their isolated, credential-free installation.

Package acquisition may use PyPI for declared Python dependencies. The installed
smoke has a fresh home, disabled provider executables and a network-denying Python
audit hook; it never installs provider runtimes or tests provider authentication.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.resources
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import struct
import subprocess
import sys
import tarfile
import tomllib
import venv
from email.parser import BytesParser
from zipfile import ZipFile


VERSION = "0.7.2"
PROBE_MODULES = (
    "supervisor", "supervisor.main", "supervisor.controller", "supervisor.project_config",
    "supervisor.prompts.supervisor", "supervisor.runtime.client", "supervisor.runtime.codex",
    "supervisor.runtime.claude", "supervisor.runtime.windows_sandbox",
)
REQUIRED = (
    "supervisor/prompts/prompts.toml",
    "supervisor/runtime/tools.json",
    "supervisor/pi_worker/package.json",
    "supervisor/pi_worker/package-lock.json",
    "supervisor/pi_worker/worker.mjs",
    "supervisor/pi_worker/src/runtime.mjs",
)
WINDOWS_REQUIRED = (
    "supervisor/runtime/bin/bello-windows-sandbox.exe",
    "supervisor/runtime/bin/THIRD_PARTY_NOTICES.txt",
)
SDIST_REQUIRED = (
    "setup.py", "MANIFEST.in", "pyproject.toml",
    "native/windows-sandbox/Cargo.toml", "native/windows-sandbox/Cargo.lock",
    "native/windows-sandbox/THIRD_PARTY_NOTICES.txt",
    "native/windows-sandbox/src/main.rs",
)


class VerificationError(ValueError):
    """A bounded diagnostic written by this verifier, never subprocess content."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise VerificationError(reason)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def is_amd64_pe(data: bytes) -> bool:
    if len(data) < 64 or data[:2] != b"MZ":
        return False
    offset = struct.unpack_from("<I", data, 60)[0]
    return (64 <= offset and offset + 6 <= len(data) and data[offset:offset + 4] == b"PE\0\0"
            and struct.unpack_from("<H", data, offset + 4)[0] == 0x8664)


def wheel_payload(path: Path, *, windows: bool) -> dict[str, str]:
    with ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate wheel members")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata) == 1, "one wheel metadata file required")
        info = BytesParser().parsebytes(archive.read(metadata[0]))
        require(info["Name"].lower() == "bello" and info["Version"] == VERSION, "wrong package identity")
        stem = metadata[0].removesuffix(".dist-info/METADATA")
        tags = BytesParser().parsebytes(archive.read(stem + ".dist-info/WHEEL")).get_all("Tag", [])
        require(tags == (["py3-none-win_amd64"] if windows else ["py3-none-any"]), "wrong wheel platform")
        payload: dict[str, str] = {}
        for name in names:
            require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
                    and "\\" not in name, "unsafe wheel member")
            if name.endswith("/"):
                continue
            relative = name
            for kind in ("purelib", "platlib"):
                prefix = f"{stem}.data/{kind}/"
                if name.startswith(prefix):
                    relative = name[len(prefix):]
                    break
            require(relative not in payload, "duplicate installed wheel path")
            payload[relative] = digest(archive.read(name))
        for name in REQUIRED + (WINDOWS_REQUIRED if windows else ()):
            require(name in payload and payload[name] != digest(b""), "missing mandatory wheel resource")
        helper_names = [name for name in names if name.endswith(WINDOWS_REQUIRED[0])]
        if windows:
            require(len(helper_names) == 1 and is_amd64_pe(archive.read(helper_names[0])), "invalid Windows helper")
        else:
            require(not helper_names, "Windows helper in portable wheel")
        # RECORD is installer bookkeeping. All package bytes and entry-point /
        # dependency metadata must otherwise survive the sdist rebuild exactly.
        return {name: value for name, value in payload.items() if not name.endswith(".dist-info/RECORD")}


def build_check(args: argparse.Namespace) -> dict:
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source_root, text=True).strip()
    require(actual_head == args.source_sha, "source checkout differs from workflow head")
    wheels = list(args.dist_dir.glob("*.whl"))
    require(len(wheels) == 1, "exactly one built wheel required")
    payload = wheel_payload(wheels[0], windows=args.windows)
    artifacts = {path.name: file_digest(path) for path in args.dist_dir.iterdir() if path.is_file()}
    rebuilt = None
    if not args.windows:
        sdists = list(args.dist_dir.glob("*.tar.gz"))
        require(len(sdists) == 1 and args.rebuilt_dir is not None, "sdist and rebuilt wheel required")
        with tarfile.open(sdists[0], "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            require(len(names) == len(set(names)), "duplicate sdist members")
            roots = {PurePosixPath(name).parts[0] for name in names}
            require(len(roots) == 1, "one sdist root required")
            root = next(iter(roots))
            by_name = {member.name: member for member in members}
            required = (*SDIST_REQUIRED, *(str(path.relative_to(args.source_root)).replace(os.sep, "/")
                                          for path in (args.source_root / "native/windows-sandbox/src").rglob("*.rs")))
            for relative in required:
                member = by_name.get(f"{root}/{relative}")
                require(member is not None and member.isfile(), "missing sdist build input")
                require(digest(archive.extractfile(member).read()) == file_digest(args.source_root / relative),
                        "sdist build input differs from source")
        rebuilt_wheels = list(args.rebuilt_dir.glob("*.whl"))
        require(len(rebuilt_wheels) == 1, "exactly one sdist-rebuilt wheel required")
        require(wheel_payload(rebuilt_wheels[0], windows=False) == payload, "sdist-rebuilt payload differs")
        rebuilt = {"sha256": file_digest(rebuilt_wheels[0]), "payload_matches": True}
    return {"status": "PASS", "source_sha": actual_head, "version": VERSION,
            "verifier_sha256": file_digest(Path(__file__).resolve()),
            "windows": args.windows, "artifacts": artifacts, "rebuilt_wheel": rebuilt,
            "wheel_payload": payload}


def isolated_environment(work: Path, scripts: Path) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE",
               "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    home, temporary = work / "home", work / "tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    environment.update({
        "PATH": str(scripts) + os.pathsep + environment.get("PATH", ""),
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TMP": str(temporary), "TEMP": str(temporary), "TMPDIR": str(temporary),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "BELLO_SKIP_UPDATE_CHECK": "1", "BELLO_RUNTIME_DIR": str(home / "runtime"),
        "BELLO_NODE": str(home / "absent-node"), "BELLO_CODEX_BINARY": str(home / "absent-codex"),
    })
    return environment


def run_logged(command: list[str], label: str, *, cwd: Path, environment: dict[str, str], output: Path,
               timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=timeout)
    (output / f"{label}.stdout.log").write_bytes(result.stdout)
    (output / f"{label}.stderr.log").write_bytes(result.stderr)
    require(result.returncode == 0, f"{label} failed")
    return result


def deny_network(event: str, _args: tuple) -> None:
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("network forbidden in installed-package smoke")


def installed_probe(args: argparse.Namespace) -> dict:
    sys.addaudithook(deny_network)
    require(sys.prefix != sys.base_prefix and sys.flags.isolated == 1, "isolated venv required")
    require(not Path.cwd().is_relative_to(args.source_root), "smoke cwd inside source")
    imported = {}
    for name in PROBE_MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(Path(sys.prefix).resolve()) and not path.is_relative_to(args.source_root),
                "source or global package imported")
        imported[name] = file_digest(path)
    supervisor = sys.modules["supervisor"]
    require(supervisor.__version__ == importlib.metadata.version("Bello") == VERSION, "installed version mismatch")
    package_parent = Path(supervisor.__file__).resolve().parent.parent
    expected = json.loads(args.payload.read_text(encoding="utf-8"))
    package_files = {name: sha for name, sha in expected.items() if name.startswith("supervisor/")}
    require(package_files and all(file_digest(package_parent / name) == sha for name, sha in package_files.items()),
            "installed package bytes differ from wheel")
    prompts = importlib.resources.files("supervisor.prompts").joinpath("prompts.toml").read_bytes()
    require(bool(tomllib.loads(prompts.decode("utf-8"))), "invalid bundled prompts")
    package = json.loads((package_parent / "supervisor/pi_worker/package.json").read_text(encoding="utf-8"))
    lock = json.loads((package_parent / "supervisor/pi_worker/package-lock.json").read_text(encoding="utf-8"))
    require(package["version"] == lock["version"] == lock["packages"][""]["version"] == VERSION,
            "Pi package identity mismatch")
    require(bool(json.loads((package_parent / "supervisor/runtime/tools.json").read_text(encoding="utf-8"))),
            "invalid bundled tools")
    from supervisor.main import cli
    doctor_output = io.StringIO()
    with contextlib.redirect_stdout(doctor_output), contextlib.redirect_stderr(doctor_output):
        doctor_code = cli.main(args=["doctor"], prog_name="bello", standalone_mode=False)
    doctor_text = doctor_output.getvalue()
    (args.report.parent / "doctor.log").write_text(doctor_text, encoding="utf-8")
    require(doctor_code == 0 and "[FAIL]" not in doctor_text and "Bello update check skipped" in doctor_text,
            "offline doctor failed")
    native = None
    if sys.platform == "win32":
        helper = package_parent / WINDOWS_REQUIRED[0]
        require(is_amd64_pe(helper.read_bytes()), "installed helper is not AMD64 PE")
        result = subprocess.run([str(helper), "host-status"], capture_output=True, timeout=30, check=True)
        status = json.loads(result.stdout)
        require(status.get("protocolVersion") == 1 and status.get("kind") == "hostPreparation"
                and status.get("operation") == "status"
                and status.get("changed") is False and type(status.get("prepared")) is bool,
                "invalid read-only native helper status")
        native = {"sha256": file_digest(helper), "host_status_exit": result.returncode,
                  "changed": False, "prepared": status["prepared"]}
    return {"status": "PASS", "version": VERSION, "python": platform.python_version(),
            "platform": platform.system(), "isolated": True, "installed_imports": imported,
            "package_files_verified": len(package_files), "doctor_exit": doctor_code,
            "doctor_warning_count": doctor_text.count("[WARN]"), "python_network_denied": True,
            "provider_authentication_tested": False, "native_helper": native}


def clean_install(args: argparse.Namespace) -> dict:
    require(not args.work_dir.is_relative_to(args.source_root), "work directory must be outside source")
    args.work_dir.mkdir(parents=True, exist_ok=False)
    cwd = args.work_dir / "cwd"
    cwd.mkdir()
    built = json.loads(args.build_report.read_text(encoding="utf-8"))
    require(built["status"] == "PASS" and built["source_sha"] == args.source_sha and built["version"] == VERSION,
            "build receipt identity mismatch")
    require(built.get("verifier_sha256") == file_digest(Path(__file__).resolve()), "verifier changed since build")
    require(built["windows"] == (sys.platform == "win32"), "build platform mismatch")
    wheels = list(args.wheel_dir.glob("*.whl"))
    require(len(wheels) == 1, "exactly one input wheel required")
    wheel = wheels[0]
    wheel_sha = file_digest(wheel)
    require(built["artifacts"].get(wheel.name) == wheel_sha, "wheel differs from build receipt")
    payload = wheel_payload(wheel, windows=sys.platform == "win32")
    require(payload == built["wheel_payload"], "wheel payload differs from build receipt")
    payload_path = args.work_dir / "wheel-payload.json"
    payload_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    environment_root = args.work_dir / "venv"
    venv.EnvBuilder(with_pip=True).create(environment_root)
    scripts = environment_root / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    bello = scripts / ("bello.exe" if sys.platform == "win32" else "bello")
    environment = isolated_environment(args.work_dir, scripts)
    run_logged([str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                "--no-input", "--no-cache-dir", str(wheel)], "pip-install", cwd=cwd,
               environment=environment, output=args.work_dir, timeout=300)
    for label, command in (
        ("module-help", [str(python), "-I", "-m", "supervisor.main", "--help"]),
        ("console-help", [str(bello), "--help"]),
    ):
        result = run_logged(command, label, cwd=cwd, environment=environment, output=args.work_dir)
        require(b"Usage:" in result.stdout and b"doctor" in result.stdout, "incomplete CLI help")
    probe_report = args.work_dir / "installed-probe.json"
    run_logged([str(python), "-I", str(Path(__file__).resolve()), "probe", "--source-root", str(args.source_root),
                "--payload", str(payload_path), "--report", str(probe_report)], "installed-probe", cwd=cwd,
               environment=environment, output=args.work_dir)
    probe = json.loads(probe_report.read_text(encoding="utf-8"))
    require(probe["status"] == "PASS" and file_digest(wheel) == wheel_sha, "installed smoke failed or wheel changed")
    return {"status": "PASS", "source_sha": args.source_sha, "wheel_sha256": wheel_sha,
            "build_report_sha256": file_digest(args.build_report), "probe_report_sha256": file_digest(probe_report),
            "console_help_exit": 0, "module_help_exit": 0, "probe": probe,
            "limitations": ["Package smoke only; no provider authentication, model calls or sandbox execution proof."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("build-check", "clean-install", "probe"):
        command = sub.add_parser(mode)
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--report", type=Path, required=True)
        if mode != "probe":
            command.add_argument("--source-sha", required=True)
        if mode == "build-check":
            command.add_argument("--dist-dir", type=Path, required=True)
            command.add_argument("--rebuilt-dir", type=Path)
            command.add_argument("--windows", action="store_true")
        elif mode == "clean-install":
            command.add_argument("--wheel-dir", type=Path, required=True)
            command.add_argument("--build-report", type=Path, required=True)
            command.add_argument("--work-dir", type=Path, required=True)
        else:
            command.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.resolve())
    try:
        result = {"build-check": build_check, "clean-install": clean_install, "probe": installed_probe}[args.mode](args)
    except Exception as exc:
        result = {"status": "FAIL", "phase": args.mode, "error_type": type(exc).__name__}
        if isinstance(exc, VerificationError):
            result["reason"] = str(exc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "phase": args.mode}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
