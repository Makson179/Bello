"""Source-candidate identity is separate from published installers and proofs."""
import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest

from scripts import prepare_native_codex_candidate as candidate
from scripts import prepare_native_codex_windows as legacy


@pytest.fixture
def input_root(tmp_path):
    root = tmp_path / "inputs"
    for name in candidate.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture input\n")
    return root


def test_candidate_identity_is_source_only_and_preserves_legacy_version(input_root):
    report = candidate.identity(input_root)
    assert report["schema"] == "bello.native-codex-source-candidate.v1"
    assert report["version"] == "0.161.0"
    assert report["upstream_revision"] == "979011409de0a60b52f179721948e65531d26144"
    assert report["published"] is False
    assert report["qualification"] == "source-only; native build and proofs required"
    assert "binary_sha256" not in report
    assert "url" not in json.dumps(report)
    assert legacy.VERSION == "0.155.1"
    assert legacy.UPSTREAM_REVISION == "be2951ea34f0d295ed0becf97079f92fa5f6950e"
    assert candidate.identity(input_root) == report


@pytest.mark.parametrize("name", candidate.INPUTS)
def test_every_preparation_input_changes_source_identity(input_root, name):
    before = candidate.identity(input_root)
    path = input_root / name
    path.write_text(path.read_text() + "changed\n")
    assert candidate.identity(input_root)["source_key"] != before["source_key"]


def test_identity_normalizes_crlf_transport_only(input_root):
    before = candidate.identity(input_root)
    patch = input_root / candidate.PATCH
    patch.write_bytes(patch.read_bytes().replace(b"\n", b"\r\n"))
    assert candidate.identity(input_root) == before


def test_missing_patch_cannot_produce_an_identity(input_root):
    (input_root / candidate.PATCH).unlink()
    with pytest.raises(FileNotFoundError):
        candidate.identity(input_root)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
def test_linked_input_is_not_accepted(input_root, tmp_path):
    path = input_root / candidate.PATCH
    path.unlink()
    target = tmp_path / "external.patch"
    target.write_text("outside")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        candidate.identity(input_root)


def test_directory_is_not_accepted_as_patch(input_root):
    path = input_root / candidate.PATCH
    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        candidate.identity(input_root)


def test_lock_normalization_candidate_and_legacy_are_distinct():
    original = ('version = 4\n\n[[package]]\nname = "codex-core"\nversion = "0.0.0"\n'
                'dependencies = ["sha2 0.10.9"]\n\n[[package]]\nname = "external"\n'
                'version = "0.0.0"\nsource = "registry+pinned"\nchecksum = "abc"\n'
                '\n[[package]]\nname = "already-versioned"\nversion = "9.8.7"\n')
    updated = legacy.normalize_workspace_versions(original, version=candidate.VERSION)
    packages = tomllib.loads(updated)["package"]
    assert packages[0]["version"] == "0.161.0"
    assert packages[1:] == tomllib.loads(original)["package"][1:]
    assert legacy.normalize_workspace_versions(updated, version=candidate.VERSION) == updated
    assert tomllib.loads(legacy.normalize_workspace_versions(original))["package"][0]["version"] == "0.155.1"


@pytest.mark.parametrize("version", ["latest", "0.161", '0.161.0"\ninvalid = true'])
def test_lock_normalization_rejects_non_release_version(version):
    with pytest.raises(ValueError, match="exact release version"):
        legacy.normalize_workspace_versions("version = 4\n", version=version)


@pytest.mark.parametrize("revision,dirty", [
    ("be2951ea34f0d295ed0becf97079f92fa5f6950e", ""),
    ("a" * 40, ""),  # A tar archive's locally created Git commit is not upstream.
    (candidate.UPSTREAM_REVISION, " M file.txt\n"),
])
def test_candidate_refuses_wrong_or_dirty_source(input_root, tmp_path, monkeypatch, revision, dirty):
    replies = iter([revision, dirty])
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: next(replies))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not apply patch"))
    with pytest.raises(ValueError):
        candidate.prepare(tmp_path / "source", root=input_root)


@pytest.mark.parametrize("crlf", [False, True])
def test_candidate_preparation_with_real_git_preserves_patch_and_external_dependencies(
        input_root, tmp_path, monkeypatch, crlf):
    source = tmp_path / "source"
    (source / "codex-rs").mkdir(parents=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True)

    git("init", "--quiet")
    git("config", "core.autocrlf", "false")
    lock = source / "codex-rs/Cargo.lock"
    lock.write_text('version = 4\n\n[[package]]\nname = "codex-core"\nversion = "0.0.0"\ndependencies = []\n'
                    '\n[[package]]\nname = "external"\nversion = "0.0.0"\nsource = "registry+pinned"\n')
    (source / "file.txt").write_text("before\n\ncontext\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
    monkeypatch.setattr(candidate, "UPSTREAM_REVISION", git("rev-parse", "HEAD").strip())
    payload = (b"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
               b"@@ -1,3 +1,3 @@\n-before\n+after\n\n context\n"
               b"diff --git a/codex-rs/Cargo.lock b/codex-rs/Cargo.lock\n"
               b"--- a/codex-rs/Cargo.lock\n+++ b/codex-rs/Cargo.lock\n"
               b'@@ -3,5 +3,5 @@\n [[package]]\n name = "codex-core"\n version = "0.0.0"\n'
               b'-dependencies = []\n+dependencies = ["external"]\n \n')
    if crlf:
        payload = payload.replace(b"\n", b"\r\n")
    patch = input_root / candidate.PATCH
    patch.write_bytes(payload)
    report = candidate.prepare(source, root=input_root)
    assert report == candidate.identity(input_root)
    assert report["version"] == "0.161.0"
    assert (source / "file.txt").read_text() == "after\n\ncontext\n"
    packages = tomllib.loads(lock.read_text())["package"]
    assert packages[0]["version"] == "0.161.0"
    assert packages[0]["dependencies"] == ["external"]
    assert packages[1]["version"] == "0.0.0"
    assert patch.read_bytes() == payload
    with pytest.raises(ValueError, match="fresh upstream"):
        candidate.prepare(source, root=input_root)


def test_candidate_reports_inputs_changing_during_application(input_root, tmp_path, monkeypatch):
    def apply(*args, **kwargs):
        assert kwargs == {"expected_revision": candidate.UPSTREAM_REVISION, "version": candidate.VERSION}
        (input_root / candidate.PATCH).write_text("changed during preparation")
    monkeypatch.setattr(legacy, "prepare", apply)
    with pytest.raises(ValueError, match="inputs changed"):
        candidate.prepare(tmp_path / "source", root=input_root)
