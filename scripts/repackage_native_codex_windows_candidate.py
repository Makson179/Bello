#!/usr/bin/env python3
"""Recover one exact Windows build and qualify a newly packaged archive; no build/release."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPO = "Makson179/Bello"
PROVENANCE = "de7aa1f89ef9ab624a66cba0a22c6012a5e156ac"
BUILD_KEY = "1ac0da04c32e0827b77e5f74375ed93238e8d4d9e8ae2ff4a1f1bc83918c1dcc"
INPUTS = {
    ".github/workflows/native-codex-windows-build.yml": "72f490ffbc96a281662a1d8e1570c301b89f9c773a85d7e37d5f62ea096efa07",
    "scripts/native-codex-selection.patch": "8d40657636bf2d8780506ee6ba8397b5bf6479bb25adc6128caab0839388df3d",
    "scripts/prepare_native_codex_windows.py": "353c51866dc0af550e5bc30a3a80ce3d8e4ff85194723d0e7435f846184b08af",
    "scripts/build_native_codex_windows.py": "7a0fd00777147d220f5eccbea81deac4f656b1232d15b675697ab6ca36412158",
}
FILES = {
    "BUILD-INFO": "bad2543f7501420c5d7927bece097e6f71eb5250614d3e4b8e0e467d2bc5f1f5",
    "LICENSE": "aa5e89edcbbd01fc3fb188a527d8bdc0da5812305cab220c84348c14ea427288",
    "NOTICE": "3c505dc54be731583470ef3584e5cb96d60df7add3e7e36294cf4dea8316a5cb",
    "THIRD-PARTY-NOTICES": "2d60425bf0ec8fc53b342fe2a6822144d83acea06500280422de6d5aaed03542",
    "bin/codex-code-mode-host.exe": "aeaab74c10458ba3959b3da27cb357ae40ed41205ae8d536722838598019e52c",
    "bin/codex-command-runner.exe": "6b30a7056b1a4885cd991044eead3422afd904de097fea61fcb2fbcb99d464c0",
    "bin/codex-windows-sandbox-setup.exe": "19c4bc6edd6648a3494e6eb86e0a3511805001cad4f7947ecdc7e1a3152638c7",
    "bin/codex.exe": "9f28f801d91fc24cb381a4d77a0776af6f04dac5621ebb0cc6d7ade8507bb6c7",
    "native-codex-selection.patch": "059c99dbec0b7e2b6201b1992c593f3af001d1dd1e264e7ae5987ac035ce47f0",
    "native-build-receipt.json": "de1f8dac77e8e9a24aeb8a8913e664b427bdf8cae7da47ab41457c306f2375f0",
}
CANDIDATE = {
    "id": 10914618171, "run": 36259350800,
    "name": "bello-native-windows-candidate-36259350800-1",
    "head": "6490cbe9c0c1b0292506dab80d27b9a8f562b952", "size": 155562905,
    "sha256": "58604a8c5b73b5cc0b0440d517cc8034559bcda5cb9885ac79827d407f41af1a",
}
PROOFS = {
    "id": 10925678478, "run": 36302796371,
    "name": "windows-root-read-proof-de7aa1f89ef9ab624a66cba0a22c6012a5e156ac-1",
    "head": PROVENANCE, "size": 958188,
    "sha256": "323917d13658f703df493dd595d4dfa50ac8eb826fd1f87192be01761b75b595",
}
PROOF_NAME = "native-root-read-proof/report.json"
PROOF_SHA = "288dfcd51729f2c2b80bfd5233974cd4c1cd5f64ec58fe667ef83a1efafcabb8"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ordinary(path: Path) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400, "Nonordinary input")


def clean_environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "LOCALAPPDATA", "USERNAME", "RUNNER_TEMP", "LANG", "LC_ALL"}
    return {**{k: v for k, v in os.environ.items() if k.upper() in allowed},
            "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1"}


def validate_provenance(historical: Path) -> None:
    env = clean_environment()
    head = subprocess.check_output(["git", "-C", str(historical), "rev-parse", "HEAD"], env=env, text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(historical), "status", "--porcelain", "--untracked-files=all"], env=env, text=True)
    require(head == PROVENANCE and not dirty.strip(), "Historical checkout identity changed")
    for name, expected in INPUTS.items():
        path = historical / name
        ordinary(path)
        require(hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == expected,
                "Historical source pin mismatch")


def validate_artifact(metadata: dict, run: dict, spec: dict) -> None:
    require(metadata.get("id") == spec["id"] and metadata.get("name") == spec["name"]
            and metadata.get("size_in_bytes") == spec["size"] and metadata.get("expired") is False
            and metadata.get("digest") == "sha256:" + spec["sha256"]
            and metadata.get("workflow_run", {}).get("id") == spec["run"]
            and metadata.get("workflow_run", {}).get("head_sha") == spec["head"]
            and run.get("id") == spec["run"] and run.get("head_sha") == spec["head"]
            and run.get("status") == "completed", "Artifact/run binding mismatch")
    # The original build survived a later proof failure; only CI03 is an acceptance run.
    if spec == PROOFS:
        require(run.get("conclusion") == "success", "Historical proof run did not pass")


def api(path: str) -> dict:
    data = subprocess.check_output(["gh", "api", f"repos/{REPO}/{path}"], timeout=60)
    require(len(data) <= 1024 * 1024, "Oversized GitHub metadata")
    return json.loads(data)


def fetch(spec: dict, target: Path) -> None:
    validate_artifact(api(f"actions/artifacts/{spec['id']}"), api(f"actions/runs/{spec['run']}"), spec)
    with target.open("xb") as stream:
        subprocess.run(["gh", "api", f"repos/{REPO}/actions/artifacts/{spec['id']}/zip"],
                       stdout=stream, check=True, timeout=180)
    require(target.stat().st_size == spec["size"] and digest(target) == spec["sha256"],
            "Downloaded archive pin mismatch")


def extract_inputs(candidate_zip: Path, proof_zip: Path, output: Path) -> None:
    require(digest(candidate_zip) == CANDIDATE["sha256"] and digest(proof_zip) == PROOFS["sha256"],
            "Input archive digest mismatch")
    with zipfile.ZipFile(candidate_zip) as archive:
        entries = [row for row in archive.infolist() if not row.is_dir()]
        require(len(entries) == len(FILES) and {row.filename for row in entries} == set(FILES)
                and all(row.filename == "bin/" for row in archive.infolist() if row.is_dir()),
                "Candidate membership mismatch")
        for row in entries:
            require(not stat.S_ISLNK(row.external_attr >> 16) and not row.flag_bits & 1
                    and row.file_size <= 512 * 1024**2, "Invalid candidate member")
            with archive.open(row) as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == FILES[row.filename],
                        "Candidate member pin mismatch")
        candidate = output / "candidate"
        candidate.mkdir()
        (candidate / "bin").mkdir()
        for row in entries:
            with archive.open(row) as source, (candidate / row.filename).open("xb") as target:
                shutil.copyfileobj(source, target)
    with zipfile.ZipFile(proof_zip) as archive:
        rows = [row for row in archive.infolist() if row.filename == PROOF_NAME]
        require(len(rows) == 1 and not stat.S_ISLNK(rows[0].external_attr >> 16)
                and rows[0].file_size <= 16 * 1024**2, "Proof member mismatch")
        body = archive.read(rows[0])
        require(hashlib.sha256(body).hexdigest() == PROOF_SHA, "Historical proof pin mismatch")
        with (output / "selection-proof.json").open("xb") as stream:
            stream.write(body)


def download(historical: Path, output: Path) -> None:
    validate_provenance(historical)
    output.mkdir(parents=True, exist_ok=False)
    fetch(CANDIDATE, output / "candidate.zip")
    fetch(PROOFS, output / "proofs.zip")
    extract_inputs(output / "candidate.zip", output / "proofs.zip", output)


def package(historical: Path, inputs: Path, output: Path) -> None:
    validate_provenance(historical)
    for name, expected in FILES.items():
        ordinary(inputs / "candidate" / name)
        require(digest(inputs / "candidate" / name) == expected, "Candidate changed after download")
    ordinary(inputs / "selection-proof.json")
    require(digest(inputs / "selection-proof.json") == PROOF_SHA, "Historical proof changed")
    command = [sys.executable, "-B", str(historical / "scripts/build_native_codex_windows.py")]
    subprocess.run([*command, "verify-build", "--candidate", str(inputs / "candidate")],
                   cwd=historical, env=clean_environment(), check=True, timeout=180)
    subprocess.run([*command, "package-candidate", "--candidate", str(inputs / "candidate"),
                    "--proof", str(inputs / "selection-proof.json"), "--output", str(output)],
                   cwd=historical, env=clean_environment(), check=True, timeout=300)
    validate_provenance(historical)
    (output / "provenance.json").write_text(json.dumps({"schema": "bello.windows-repackage.v1",
        "historical_checkout": PROVENANCE, "historical_build_key": BUILD_KEY,
        "candidate_artifact": CANDIDATE, "proof_artifact": PROOFS, "proof_sha256": PROOF_SHA,
        "native_recompiled": False, "new_archive_not_original_ci03_bytes": True,
        "published": False}, indent=2) + "\n", encoding="utf-8", newline="\n")


def qualify(artifact: Path, runtime_root: Path, output: Path) -> None:
    from scripts import build_native_codex_windows as build
    from scripts.verify_native_codex_async import verify
    from scripts.verify_native_codex_async_download import validate_proof
    from scripts.verify_native_codex_download import snapshot
    from supervisor.runtime import native_codex_install as installer

    output.mkdir(parents=True, exist_ok=False)
    result = {"passed": False, "paid_model_calls": 0, "published": False, "phase": "install"}
    clean = clean_environment()
    clean.update(HOME=str(runtime_root / "empty-home"), USERPROFILE=str(runtime_root / "empty-home"),
                 CODEX_HOME=str(runtime_root / "empty-home"), BELLO_RUNTIME_DIR=str(runtime_root),
                 APPDATA=str(runtime_root / "empty-home/config"),
                 XDG_CONFIG_HOME=str(runtime_root / "empty-home/config"),
                 XDG_DATA_HOME=str(runtime_root / "empty-home/data"),
                 XDG_CACHE_HOME=str(runtime_root / "empty-home/cache"))
    try:
        require(platform.system() == "Windows" and platform.machine().lower() in {"amd64", "x86_64"},
                "Qualification requires Windows x64")
        require(not os.path.lexists(runtime_root), "Qualification requires a fresh runtime")
        provenance = json.loads((artifact / "provenance.json").read_text())
        require(provenance.get("historical_checkout") == PROVENANCE
                and provenance.get("historical_build_key") == BUILD_KEY
                and provenance.get("candidate_artifact") == CANDIDATE
                and provenance.get("proof_artifact") == PROOFS
                and provenance.get("proof_sha256") == PROOF_SHA
                and provenance.get("native_recompiled") is False
                and provenance.get("published") is False, "Packaged provenance mismatch")
        checksums = json.loads((artifact / "checksums.json").read_text())
        packaged_manifest = artifact / "bundle/selection-manifest.json"
        require(digest(packaged_manifest) == checksums["manifest_sha256"], "Packaged manifest mismatch")
        file_pins = json.loads(packaged_manifest.read_text())["files"]
        require(set(file_pins) == set(FILES) - {"native-build-receipt.json"}, "Packaged member mismatch")
        for name, expected in FILES.items():
            if name not in {"BUILD-INFO", "native-build-receipt.json"}:
                require(file_pins[name] == expected, "Packaged original payload mismatch")
        info = json.loads((artifact / "bundle/BUILD-INFO").read_text())
        require(digest(artifact / "bundle/BUILD-INFO") == file_pins["BUILD-INFO"]
                and info.get("provider_proof_sha256") == PROOF_SHA
                and info.get("native_build_identity", {}).get("build_key") == BUILD_KEY,
                "Packaged BUILD-INFO provenance mismatch")
        with patch.dict(os.environ, clean, clear=True):
            # This is final-checkout code. Its install path does not relabel the historical build key.
            build.install_local(artifact, runtime_root, output / "selection")
            receipt = json.loads((runtime_root / "installed-cache.json").read_text())
            expected = runtime_root / "native-codex" / checksums["archive_sha256"]
            binary, manifest = expected / "bin/codex.exe", expected / "selection-manifest.json"
            require(receipt.get("passed") is True and receipt.get("post_proof_cache_reusable") is True
                    and Path(receipt.get("binary", "")).resolve() == binary.resolve()
                    and receipt.get("archive_sha256") == checksums["archive_sha256"]
                    and receipt.get("manifest_sha256") == checksums["manifest_sha256"]
                    and receipt.get("binary_sha256") == FILES["bin/codex.exe"]
                    and digest(binary) == FILES["bin/codex.exe"], "Installed binary/receipt binding mismatch")
            for name, expected_hash in FILES.items():
                if name.startswith("bin/"):
                    require(digest(expected / name) == expected_hash, "Installed native companion changed")
            before = snapshot(expected)
            result["phase"] = "async"
            proof = asyncio.run(verify(binary, output / "async", concurrency_repeats=3))
            validate_proof(proof)
            bundle = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/ci-local-only/" + checksums["archive"],
                                            checksums["archive_sha256"], checksums["manifest_sha256"])
            def no_download(*args):
                raise ValueError("Post-proof cache attempted a new download")
            with patch.dict(installer.ASYNC_BUNDLES, {("Windows", "x86_64"): bundle}, clear=True), patch.object(installer, "_download", no_download):
                command, found_manifest = installer.ensure_native_async()
            require(Path(command[0]).resolve() == binary.resolve() and found_manifest == manifest
                    and snapshot(expected) == before, "Installed cache changed during async proof")
            shutil.copyfile(runtime_root / "installed-cache.json", output / "installed-cache.json")
            result.update(passed=True, phase="complete", selection_cases=9, async_cases=13,
                          archive_sha256=checksums["archive_sha256"], manifest_sha256=checksums["manifest_sha256"],
                          binary_sha256=digest(binary), cache_unchanged=True,
                          selection_report_sha256=digest(output / "selection/report.json"),
                          async_report_sha256=digest(output / "async/report.json"))
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        (output / "report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, arguments in (("download", ("historical", "output")),
                            ("package", ("historical", "inputs", "output")),
                            ("qualify", ("artifact", "runtime-root", "output"))):
        command = sub.add_parser(name)
        for argument in arguments:
            command.add_argument("--" + argument, required=True, type=lambda value: Path(value).absolute())
    args = vars(parser.parse_args())
    globals()[args.pop("command")](**args)


if __name__ == "__main__":
    main()
