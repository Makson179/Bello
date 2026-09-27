"""Offline contracts for one preserved build; no download, native execution or publication."""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import zipfile

import pytest

from scripts import repackage_native_codex_windows_candidate as repack


def test_manual_workflow_has_two_distinct_checkouts_and_no_build_or_publication():
    text = (repack.ROOT / ".github/workflows/native-codex-windows-repackage.yml").read_text()
    assert "workflow_dispatch:" in text and "\n  push:" not in text and "\n  pull_request:" not in text
    assert "contents: read" in text and "actions: read" in text
    assert "runs-on: windows-2025" in text and "timeout-minutes: 25" in text
    assert text.count("persist-credentials: false") == 2
    assert "path: final" in text and "working-directory: final" in text
    assert f"ref: {repack.PROVENANCE}" in text and "path: provenance" in text
    assert text.count("GH_TOKEN:") == 1 and "secrets." not in text
    assert not any(term in text for term in ("cargo ", "gh release", "publish:", "write-all", "npm "))
    assert "--historical ../provenance --inputs ../repackage-inputs" in text
    assert '--artifact "$env:GITHUB_WORKSPACE/native-repackaged"' in text
    assert "native-repackaged/*.tar.gz" in text and "native-repackaged/bundle/selection-manifest.json" in text
    assert "repackage-proof/async/report.json" in text and "repackage-proof/selection/report.json" in text
    for name in ("test_native_codex_windows_repackage.py", "test_native_codex_async_download.py", "test_build_native_codex_windows.py"):
        assert (repack.ROOT / "tests" / name).is_file()


def test_qualification_paths_have_no_parent_components_for_native_setup():
    text = (repack.ROOT / ".github/workflows/native-codex-windows-repackage.yml").read_text()
    command = next(line.strip() for line in text.splitlines()
                   if "repackage_native_codex_windows_candidate.py qualify " in line)
    workspace = PureWindowsPath(r"D:\a\Bello\Bello")
    # Path.absolute() retained this old ParentDir; native's no-reparse validator
    # rejects it before the helper opens its log or writes a structured report.
    old_home = workspace / "final" / ".." / "repackage-proof" / "selection" / "direct_on" / "empty-home"
    assert old_home.is_absolute() and ".." in old_home.parts
    for flag, leaf in (("artifact", "native-repackaged"), ("output", "repackage-proof")):
        argument = f'--{flag} "$env:GITHUB_WORKSPACE/{leaf}"'
        assert argument in command
        actual = PureWindowsPath(str(workspace) + "/" + leaf)
        assert actual.is_absolute() and ".." not in actual.parts
        assert actual == workspace / leaf
    assert "../" not in command and "Resolve-Path" not in command


def test_failure_retention_is_unqualified_and_excludes_private_setup_state(tmp_path):
    text = (repack.ROOT / ".github/workflows/native-codex-windows-repackage.yml").read_text()
    failure = text.split("      - name: Preserve UNQUALIFIED archive and scoped setup diagnostics\n")[1]
    assert "if: failure()" in failure
    assert "name: native-windows-repackage-UNQUALIFIED-${{ github.sha }}-${{ github.run_attempt }}" in failure
    paths = {line.strip() for line in failure.split("          path: |\n")[1].split("          if-no-files-found:")[0].splitlines()}
    assert paths == {
        "native-repackaged/*.tar.gz", "native-repackaged/bundle/selection-manifest.json",
        "native-repackaged/checksums.json", "native-repackaged/provenance.json",
        "repackage-proof/report.json", "repackage-proof/selection/report.json",
        "repackage-proof/async/report.json",
        "repackage-proof/selection/*/windows-setup-stdout.txt",
        "repackage-proof/selection/*/windows-setup-stderr.txt",
    }
    allowed = {
        "native-repackaged/candidate.tar.gz", "native-repackaged/bundle/selection-manifest.json",
        "repackage-proof/selection/direct_on/windows-setup-stdout.txt",
        "repackage-proof/selection/direct_on/windows-setup-stderr.txt",
    }
    forbidden = {
        "repackage-proof/selection/direct_on/empty-home/.sandbox/secrets.json",
        "repackage-proof/selection/direct_on/empty-home/auth.json",
        "repackage-proof/selection/direct_on/raw-rpc.jsonl",
        "repackage-proof/selection/direct_on/empty-home/windows-setup-stderr.txt",
        "native-repackaged/bundle/auth.json",
    }
    for name in allowed | forbidden:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture only")
    selected = {file.relative_to(tmp_path).as_posix() for pattern in paths for file in tmp_path.glob(pattern)}
    assert selected == allowed


@pytest.fixture
def artifact():
    spec = repack.CANDIDATE
    metadata = {"id": spec["id"], "name": spec["name"], "size_in_bytes": spec["size"],
                "expired": False, "digest": "sha256:" + spec["sha256"],
                "workflow_run": {"id": spec["run"], "head_sha": spec["head"]}}
    return metadata, {"id": spec["run"], "head_sha": spec["head"], "status": "completed", "conclusion": "failure"}


def test_preserved_candidate_from_failed_later_proof_is_not_claimed_acceptance(artifact):
    repack.validate_artifact(*artifact, repack.CANDIDATE)


@pytest.mark.parametrize("field", ["id", "name", "size_in_bytes", "expired", "digest", "workflow_run", "head", "status"])
def test_artifact_identity_mixing_fails(artifact, field):
    metadata, run = artifact
    if field == "head": run["head_sha"] = "f" * 40
    elif field == "status": run["status"] = "in_progress"
    else: metadata[field] = {} if field == "workflow_run" else "wrong"
    with pytest.raises(ValueError): repack.validate_artifact(metadata, run, repack.CANDIDATE)


def test_qualified_artifact_requires_successful_exact_run():
    spec = repack.PROOFS
    metadata = {"id": spec["id"], "name": spec["name"], "size_in_bytes": spec["size"],
                "expired": False, "digest": "sha256:" + spec["sha256"],
                "workflow_run": {"id": spec["run"], "head_sha": spec["head"]}}
    run = {"id": spec["run"], "head_sha": spec["head"], "status": "completed", "conclusion": "failure"}
    with pytest.raises(ValueError): repack.validate_artifact(metadata, run, spec)
    run["conclusion"] = "success"
    repack.validate_artifact(metadata, run, spec)


@pytest.fixture
def archives(tmp_path, monkeypatch):
    candidate, proof = tmp_path / "candidate.zip", tmp_path / "proof.zip"
    members = {"bin/codex.exe": b"synthetic executable", "native-build-receipt.json": b"synthetic receipt"}
    with zipfile.ZipFile(candidate, "w") as archive:
        for name, body in members.items(): archive.writestr(name, body)
    with zipfile.ZipFile(proof, "w") as archive:
        archive.writestr(repack.PROOF_NAME, b"synthetic proof")
        archive.writestr("../unselected-do-not-extract", b"ignored")
    monkeypatch.setattr(repack, "FILES", {name: hashlib.sha256(body).hexdigest() for name, body in members.items()})
    monkeypatch.setattr(repack, "CANDIDATE", {"sha256": repack.digest(candidate)})
    monkeypatch.setattr(repack, "PROOFS", {"sha256": repack.digest(proof)})
    monkeypatch.setattr(repack, "PROOF_SHA", hashlib.sha256(b"synthetic proof").hexdigest())
    output = tmp_path / "out"
    output.mkdir()
    return candidate, proof, output


def test_extracts_exact_candidate_and_only_pinned_proof(archives):
    repack.extract_inputs(*archives)
    assert {p.relative_to(archives[2]).as_posix() for p in archives[2].rglob("*") if p.is_file()} == {
        "candidate/bin/codex.exe", "candidate/native-build-receipt.json", "selection-proof.json"}
    assert not (archives[2].parent / "unselected-do-not-extract").exists()


@pytest.mark.parametrize("mode", ["archive", "member", "membership", "proof", "duplicate_proof", "existing"])
def test_input_corruption_and_reuse_rejected(archives, monkeypatch, mode):
    candidate, proof, output = archives
    if mode == "archive": monkeypatch.setitem(repack.CANDIDATE, "sha256", "0" * 64)
    elif mode == "member": monkeypatch.setitem(repack.FILES, "bin/codex.exe", "0" * 64)
    elif mode == "membership": monkeypatch.setitem(repack.FILES, "unexpected", "0" * 64)
    elif mode == "proof": monkeypatch.setattr(repack, "PROOF_SHA", "0" * 64)
    elif mode == "existing": (output / "candidate").mkdir()
    else:
        with zipfile.ZipFile(proof, "a") as archive:
            with pytest.warns(UserWarning): archive.writestr(repack.PROOF_NAME, b"synthetic proof")
        monkeypatch.setitem(repack.PROOFS, "sha256", repack.digest(proof))
    with pytest.raises((ValueError, FileExistsError)): repack.extract_inputs(candidate, proof, output)


@pytest.mark.parametrize("mode", ["good", "head", "dirty", "source"])
def test_historical_identity_requires_exact_clean_commit_and_pins(tmp_path, monkeypatch, mode):
    target = tmp_path / "file.py"
    target.write_bytes(b"same\r\n")
    monkeypatch.setattr(repack, "INPUTS", {"file.py": hashlib.sha256(b"same\n").hexdigest()})
    def check(command, **kwargs):
        if "rev-parse" in command: return (repack.PROVENANCE if mode != "head" else "f" * 40) + "\n"
        return " M file.py\n" if mode == "dirty" else ""
    monkeypatch.setattr(repack.subprocess, "check_output", check)
    if mode == "source": target.write_bytes(b"changed")
    if mode == "good": repack.validate_provenance(tmp_path)
    else:
        with pytest.raises(ValueError): repack.validate_provenance(tmp_path)


@pytest.mark.parametrize("mode", ["clean", "modified", "untracked"])
def test_windows_crlf_checkout_under_sanitized_git_config(tmp_path, monkeypatch, mode):
    """Real Git: accept clean CRLF checkout, reject modified or untracked files."""
    root = tmp_path / "public-fixture"
    root.mkdir()
    text = b"public fixture line\nsecond line\n"
    (root / "input.txt").write_bytes(text)
    env = repack.clean_environment()
    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(root), *arguments], env=env, text=True, stderr=subprocess.DEVNULL)
    git("init", "--quiet")
    git("add", "input.txt")
    git("-c", "user.name=Public fixture", "-c", "user.email=fixture@localhost",
        "commit", "--quiet", "-m", "public fixture")
    checkout = tmp_path / "crlf-checkout"
    git("-c", "core.autocrlf=true", "clone", "--quiet", "--local", str(root), str(checkout))
    root = checkout
    monkeypatch.setattr(repack, "PROVENANCE", git("rev-parse", "HEAD").strip())
    monkeypatch.setattr(repack, "INPUTS", {"input.txt": hashlib.sha256(text).hexdigest()})
    assert (root / "input.txt").read_bytes() == text.replace(b"\n", b"\r\n")
    assert git("-c", "core.autocrlf=true", "status", "--porcelain", "--untracked-files=all") == ""
    # Do not assert how an unconfigured status classifies identical CRLF bytes:
    # Git's stat cache can make that observation vary across filesystems.
    # The contract below tests our configured validator, including real edits.
    monkeypatch.setattr(repack.platform, "system", lambda: "Windows")
    if mode == "modified": (root / "input.txt").write_bytes(b"changed content\r\n")
    if mode == "untracked": (root / "unexpected.txt").write_text("unexpected")
    if mode == "clean": repack.validate_provenance(root)
    else:
        with pytest.raises(ValueError): repack.validate_provenance(root)


def test_clean_environment_excludes_auth_proxy_python_and_native_overrides(monkeypatch):
    for key in ("GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_HOME", "HTTPS_PROXY",
                "PYTHONPATH", "BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST"):
        monkeypatch.setenv(key, "synthetic-do-not-inherit")
    env = repack.clean_environment()
    assert "synthetic-do-not-inherit" not in env.values()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["PYTHONDONTWRITEBYTECODE"] == "1"


@pytest.fixture
def qualification(tmp_path, monkeypatch):
    from scripts import build_native_codex_windows as build, verify_native_codex_async as native
    from scripts import verify_native_codex_async_download as validator
    from supervisor.runtime import native_codex_install as installer
    artifact, runtime, output = tmp_path / "artifact", tmp_path / "runtime", tmp_path / "proof"
    artifact.mkdir()
    checksums = {"archive": "test.tar.gz", "archive_sha256": "a" * 64, "manifest_sha256": "b" * 64}
    binary = runtime / "native-codex" / checksums["archive_sha256"] / "bin/codex.exe"
    manifest = binary.parent.parent / "selection-manifest.json"
    binary_sha = hashlib.sha256(b"synthetic binary").hexdigest()
    monkeypatch.setitem(repack.FILES, "bin/codex.exe", binary_sha)
    for name in repack.FILES:
        if name.startswith("bin/") and name != "bin/codex.exe":
            monkeypatch.setitem(repack.FILES, name, hashlib.sha256(b"synthetic companion").hexdigest())
    monkeypatch.setattr(repack.platform, "system", lambda: "Windows")
    monkeypatch.setattr(repack.platform, "machine", lambda: "AMD64")
    bundle = artifact / "bundle"
    bundle.mkdir()
    info = bundle / "BUILD-INFO"
    info.write_text(json.dumps({"provider_proof_sha256": repack.PROOF_SHA,
                               "native_build_identity": {"build_key": repack.BUILD_KEY}}))
    files = {k: v for k, v in repack.FILES.items() if k != "native-build-receipt.json"}
    files["BUILD-INFO"] = repack.digest(info)
    packaged_manifest = bundle / "selection-manifest.json"
    packaged_manifest.write_text(json.dumps({"files": files}))
    checksums["manifest_sha256"] = repack.digest(packaged_manifest)
    (artifact / "checksums.json").write_text(json.dumps(checksums))
    (artifact / "provenance.json").write_text(json.dumps({"historical_checkout": repack.PROVENANCE,
        "historical_build_key": repack.BUILD_KEY, "candidate_artifact": repack.CANDIDATE,
        "proof_artifact": repack.PROOFS, "proof_sha256": repack.PROOF_SHA,
        "native_recompiled": False, "published": False}))
    state = {"artifact": artifact, "runtime": runtime, "output": output, "mode": "good", "events": []}
    def install(received, root, proof):
        assert received == artifact and root == runtime
        assert "GH_TOKEN" not in os.environ and "OPENAI_API_KEY" not in os.environ
        state["events"].append("install-final")
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic binary")
        for name in repack.FILES:
            if name.startswith("bin/") and name != "bin/codex.exe":
                (binary.parent.parent / name).write_bytes(b"synthetic companion")
        if state["mode"] == "helper":
            (binary.parent / "codex-code-mode-host.exe").write_bytes(b"foreign helper")
        manifest.write_text("synthetic manifest")
        receipt = {"passed": True, "post_proof_cache_reusable": True, "binary": str(binary),
                   "binary_sha256": binary_sha, **checksums}
        if state["mode"] == "path": receipt["binary"] = str(tmp_path / "foreign.exe")
        (runtime / "installed-cache.json").write_text(json.dumps(receipt))
        proof.mkdir()
        (proof / "report.json").write_text("synthetic selection report")
    async def verify(path, proof, *, concurrency_repeats):
        assert path == binary and concurrency_repeats == 3
        state["events"].append("async-final")
        proof.mkdir()
        report = {"schema": "bello.native-async-smoke.v1", "passed": True, "paid_model_calls": 0,
                  "on_off_native_instructions_identical": True,
                  "results": [{"case": case, "passed": True, "external_proxy_requests_forwarded": 0}
                              for case in validator.CASES]}
        if state["mode"] == "incomplete": report["results"].pop()
        if state["mode"] == "mutation": binary.write_bytes(b"mutated")
        (proof / "report.json").write_text(json.dumps(report))
        return report
    def reuse():
        state["events"].append("cache")
        if state["mode"] == "download": installer._download(None, None)
        return [str(binary), "app-server"], manifest
    monkeypatch.setattr(build, "install_local", install)
    monkeypatch.setattr(native, "verify", verify)
    monkeypatch.setattr(installer, "ensure_native_async", reuse)
    monkeypatch.setenv("GH_TOKEN", "synthetic-github")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider")
    return state


@pytest.mark.parametrize("mode", ["good", "path", "helper", "incomplete", "mutation", "download", "provenance", "existing", "manifest"])
def test_fresh_final_qualification_and_failure_receipts(qualification, mode):
    state = qualification
    state["mode"] = mode
    before = dict(os.environ)
    if mode == "existing": state["runtime"].mkdir()
    if mode == "provenance": (state["artifact"] / "provenance.json").write_text("{}")
    if mode == "manifest": (state["artifact"] / "bundle/selection-manifest.json").write_text("{}")
    if mode == "good": repack.qualify(state["artifact"], state["runtime"], state["output"])
    else:
        with pytest.raises(ValueError): repack.qualify(state["artifact"], state["runtime"], state["output"])
    report = json.loads((state["output"] / "report.json").read_text())
    assert report["passed"] is (mode == "good") and report["paid_model_calls"] == 0
    assert os.environ == before
    if mode == "good": assert state["events"] == ["install-final", "async-final", "cache"]
    if mode in {"path", "helper"}: assert state["events"] == ["install-final"]
    if mode in {"provenance", "existing", "manifest"}: assert not state["events"]
