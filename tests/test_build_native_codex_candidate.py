"""Unpublished native candidate capture is tested without compilation/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from scripts import build_native_codex_candidate as build
from scripts import prepare_native_codex_candidate as prepare
from scripts import prepare_native_codex_windows as legacy


def elf(payload=b""):
    data = bytearray(256)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 3, 62, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HHH", data, 52, 64, 56, 1)
    struct.pack_into("<IIQ", data, 64, 1, 5, 0)
    struct.pack_into("<QQ", data, 96, 256, 256)
    return bytes(data) + payload


def pe(payload=b""):
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<HH", data, 68, 0x8664, 1)
    struct.pack_into("<HH", data, 84, 112, 2)
    struct.pack_into("<H", data, 88, 0x20B)
    struct.pack_into("<II", data, 216, 256, 256)
    return bytes(data) + payload


@pytest.fixture
def recipe(tmp_path):
    root = tmp_path / "recipe"
    for name in (*prepare.INPUTS, *build.RECIPE_INPUTS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("fixture " + name + "\n").encode("utf-8"))
    (root / prepare.PATCH).write_text(
        "diff --git a/changed.txt b/changed.txt\n--- a/changed.txt\n+++ b/changed.txt\n"
        "@@ -1 +1 @@\n-before\n+after\n", encoding="utf-8", newline="\n"
    )
    return root


@pytest.fixture
def prepared_source(tmp_path, recipe, monkeypatch):
    source = tmp_path / "source"
    (source / "codex-rs/vendor/bubblewrap").mkdir(parents=True)
    (source / "codex-rs/Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "codex-core"\nversion = "0.0.0"\n'
        '\n[[package]]\nname = "external"\nversion = "0.0.0"\nsource = "registry+pinned"\n',
        encoding="utf-8", newline="\n"
    )
    (source / "codex-rs/vendor/bubblewrap/COPYING").write_text(
        "bubblewrap fixture license"
    )
    (source / "LICENSE").write_text("upstream fixture license")
    (source / "NOTICE").write_text("upstream fixture notice")
    # This synthetic Git repository explicitly disables autocrlf below, so
    # create its canonical LF source bytes on Windows as well as POSIX.
    (source / "changed.txt").write_bytes(b"before\n")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args])

    git("init", "--quiet")
    git("config", "core.autocrlf", "false")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    monkeypatch.setattr(
        prepare, "UPSTREAM_REVISION", git("rev-parse", "HEAD").decode().strip()
    )
    prepare.prepare(source, root=recipe)
    return source


@pytest.fixture
def cargo_home(tmp_path):
    path = tmp_path / "cargo"
    notice = path / "registry/src/example/dependency/LICENSE-MIT"
    notice.parent.mkdir(parents=True)
    notice.write_text("downloaded dependency fixture license")
    return path


@pytest.fixture
def factory(tmp_path, recipe, prepared_source, cargo_home, monkeypatch):
    def create(target):
        release = tmp_path / (target + "-release")
        release.mkdir()
        image = elf if target == "linux-x64" else pe
        for name in build.TARGETS[target]["executables"].values():
            (release / name).write_bytes(image(name.encode()))
        if target == "linux-x64":
            digest = build.sha256(release / "bwrap")
            (release / "codex").write_bytes(elf(digest.encode()))
            monkeypatch.setenv("CODEX_BWRAP_SHA256", digest)
        output = tmp_path / (target + "-candidate")
        build.snapshot_build(
            target, prepared_source, release, cargo_home, output, root=recipe
        )
        return output, release

    return create


@pytest.fixture(params=["linux-x64", "windows-x64"])
def captured(request, factory):
    output, release = factory(request.param)
    return request.param, output, release


def rewrite_receipt(candidate, update):
    path = candidate / build.RECEIPT
    receipt = json.loads(path.read_text())
    update(receipt)
    path.write_text(json.dumps(receipt))


def rehash(candidate, name):
    rewrite_receipt(
        candidate,
        lambda receipt: receipt["files"].update({name: build.sha256(candidate / name)}),
    )


def test_identity_platform_recipe_and_legacy_pins_are_separate(recipe):
    linux, windows = (build.identity(target, recipe) for target in build.TARGETS)
    assert linux["build_key"] != windows["build_key"]
    assert linux["version"] == windows["version"] == "0.161.0"
    assert linux["published"] is windows["published"] is False
    assert linux["v8_artifact_sha256"] == build.linux_inputs.V8_HASHES
    assert windows["v8_artifact_sha256"] == legacy.V8_HASHES
    assert linux["rust_version"] == windows["rust_version"] == "1.95.0"
    assert linux["profile"]["locked"] is True
    assert windows["profile"]["libsqlite3_flags"] == "SQLITE_DISABLE_INTRINSIC"
    assert legacy.VERSION == "0.155.1"
    assert legacy.UPSTREAM_REVISION == "be2951ea34f0d295ed0becf97079f92fa5f6950e"


@pytest.mark.parametrize("target", build.TARGETS)
@pytest.mark.parametrize("name", (*prepare.INPUTS, *build.RECIPE_INPUTS))
def test_every_named_recipe_input_invalidates_build_key(recipe, name, target):
    before = build.identity(target, recipe)
    path = recipe / name
    path.write_text(path.read_text() + "changed\n")
    assert build.identity(target, recipe)["build_key"] != before["build_key"]


def test_candidate_retains_scoped_drive_root_conversion_and_native_regressions():
    patch = (prepare.ROOT / prepare.PATCH).read_text(encoding="utf-8")
    assert "+        let path = file_url_for_native_conversion(&self.0)" in patch
    assert "+fn file_url_for_native_conversion(url: &Url) -> Cow<'_, Url>" in patch
    assert "+    let is_drive_root = url.host_str().is_none()" in patch
    assert "+            .is_some_and(is_windows_drive_uri_segment);" in patch
    for test in (
        "native_conversion_url_restores_only_bare_windows_drive_roots",
        "windows_config_and_parent_roots_keep_native_conversion_separator",
        "host_windows_drive_root_config_and_parent_round_trip",
        "host_windows_drive_root_repair_preserves_invalid_path_refusals",
    ):
        assert f"+fn {test}()" in patch


def test_transport_crlf_does_not_change_recipe_key(recipe):
    before = build.identity("windows-x64", recipe)
    for name in (*prepare.INPUTS, *build.RECIPE_INPUTS):
        path = recipe / name
        path.write_bytes(
            path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        )
    assert build.identity("windows-x64", recipe) == before


@pytest.mark.parametrize("target", ["darwin-arm64", "windows", "", "../linux-x64"])
def test_unknown_target_rejected(recipe, target):
    with pytest.raises(ValueError, match="target"):
        build.identity(target, recipe)


def test_capture_retains_exact_payload_license_lock_and_recipe(
    captured, recipe, prepared_source
):
    target, candidate, _ = captured
    receipt = build.verify_build(target, candidate, root=recipe)
    assert receipt["schema"] == build.SCHEMA and receipt["proof_status"] == "not-run"
    assert receipt["identity"] == build.identity(target, recipe)
    assert (
        set(receipt["files"])
        == set(build.TARGETS[target]["executables"]) | build.METADATA
    )
    assert (candidate / "Cargo.lock").read_bytes() == (
        prepared_source / "codex-rs/Cargo.lock"
    ).read_bytes()
    info = json.loads((candidate / "BUILD-INFO").read_text())
    assert info["cargo_lock_sha256"] == receipt["files"]["Cargo.lock"]
    assert (
        "downloaded dependency fixture license"
        in (candidate / "THIRD-PARTY-NOTICES").read_text()
    )
    assert info["published"] is False and info["proof_status"] == "not-run"
    if target == "linux-x64":
        assert (
            "bubblewrap fixture license"
            in (candidate / "THIRD-PARTY-NOTICES").read_text()
        )
        assert info["bwrap_sha256"] == receipt["files"]["bin/codex-resources/bwrap"]
    else:
        assert info["bwrap_sha256"] is None
    assert not (candidate / "selection-manifest.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "extra_directory",
        "missing",
        "hash",
        "identity",
        "status",
        "patch",
        "lock",
        "info",
        "architecture",
        "boolean",
    ],
)
def test_capture_rejects_tamper(captured, recipe, change):
    target, candidate, _ = captured
    executable = next(iter(build.TARGETS[target]["executables"]))
    if change == "extra":
        (candidate / "unlisted.txt").write_text("unexpected")
    elif change == "extra_directory":
        (candidate / "extra").mkdir()
    elif change == "missing":
        (candidate / "NOTICE").unlink()
    elif change == "hash":
        (candidate / executable).write_bytes(b"corruption")
    elif change == "identity":
        rewrite_receipt(
            candidate, lambda record: record["identity"].update({"version": "0.155.1"})
        )
    elif change == "status":
        rewrite_receipt(
            candidate, lambda record: record.update({"proof_status": "passed"})
        )
    elif change == "boolean":
        rewrite_receipt(
            candidate,
            lambda record: record["identity"]["profile"].update({"debug": False}),
        )
    elif change in {"patch", "lock", "info"}:
        name = {
            "patch": "native-codex-selection.patch",
            "lock": "Cargo.lock",
            "info": "BUILD-INFO",
        }[change]
        (candidate / name).write_text("{}" if change == "info" else "wrong")
        rehash(candidate, name)
    else:
        image = bytearray((candidate / executable).read_bytes())
        struct.pack_into(
            "<H",
            image,
            18 if target == "linux-x64" else 68,
            183 if target == "linux-x64" else 0xAA64,
        )
        (candidate / executable).write_bytes(image)
        rehash(candidate, executable)
    with pytest.raises((ValueError, FileNotFoundError)):
        build.verify_build(target, candidate, root=recipe)


def test_changed_recipe_invalidates_existing_capture(captured, recipe):
    target, candidate, _ = captured
    path = recipe / build.RECIPE_INPUTS[-1]
    path.write_text("different workflow")
    with pytest.raises(ValueError, match="identity"):
        build.verify_build(target, candidate, root=recipe)


def test_cross_platform_capture_rejected(captured, recipe):
    target, candidate, _ = captured
    other = "windows-x64" if target == "linux-x64" else "linux-x64"
    with pytest.raises(ValueError):
        build.verify_build(other, candidate, root=recipe)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fixture links/modes")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory_link"])
def test_links_rejected_before_mode_repair(captured, recipe, tmp_path, kind):
    target, candidate, _ = captured
    executable = next(iter(build.TARGETS[target]["executables"]))
    if kind == "directory_link":
        real = tmp_path / "redirected-bin"
        (candidate / "bin").rename(real)
        (candidate / "bin").symlink_to(real, target_is_directory=True)
    else:
        original = candidate / executable
        outside = tmp_path / "outside"
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside) if kind == "symlink" else os.link(
            outside, original
        )
    with pytest.raises(ValueError, match="ordinary"):
        build.verify_build(target, candidate, True, root=recipe)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission bits")
def test_restore_modes_only_after_entire_payload_is_verified(captured, recipe):
    target, candidate, _ = captured
    executables = list(build.TARGETS[target]["executables"])
    for name in executables:
        (candidate / name).chmod(0o644)
    original = (candidate / "NOTICE").read_bytes()
    (candidate / "NOTICE").write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        build.verify_build(target, candidate, True, root=recipe)
    assert all((candidate / name).stat().st_mode & 0o111 == 0 for name in executables)
    (candidate / "NOTICE").write_bytes(original)
    build.verify_build(target, candidate, True, root=recipe)
    assert all(
        (candidate / name).stat().st_mode & 0o111 == 0o111 for name in executables
    )


def test_private_source_check_never_modifies_git_index_or_objects(
    prepared_source, recipe
):
    index = prepared_source / ".git/index"
    before_index = index.read_bytes()
    objects = prepared_source / ".git/objects"
    before_objects = {
        str(path.relative_to(objects)): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    }
    build._verify_prepared_source(prepared_source, recipe / prepare.PATCH)
    assert index.read_bytes() == before_index
    assert {
        str(path.relative_to(objects)): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    } == before_objects


def test_source_check_accepts_patch_added_files(prepared_source, recipe):
    addition = (
        "diff --git a/added.txt b/added.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/added.txt\n@@ -0,0 +1 @@\n+candidate addition\n"
    )
    patch = recipe / prepare.PATCH
    patch.write_text(patch.read_text() + addition)
    subprocess.run(
        ["git", "-C", str(prepared_source), "apply", "-"],
        input=addition.encode(),
        check=True,
    )
    build._verify_prepared_source(prepared_source, patch)


def test_source_check_accepts_windows_autocrlf_transport(prepared_source, recipe):
    subprocess.run(
        ["git", "-C", str(prepared_source), "config", "core.autocrlf", "true"],
        check=True,
    )
    for name in ("changed.txt", "codex-rs/Cargo.lock"):
        path = prepared_source / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    build._verify_prepared_source(prepared_source, recipe / prepare.PATCH)
    changed = prepared_source / "changed.txt"
    original = changed.stat()
    changed.write_bytes(b"aftex\r\n")
    os.utime(changed, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(ValueError, match="source differs"):
        build._verify_prepared_source(prepared_source, recipe / prepare.PATCH)


@pytest.mark.parametrize("scope", ["global", "system"])
def test_source_check_preserves_effective_autocrlf_conversion(
    prepared_source, recipe, tmp_path, monkeypatch, scope
):
    subprocess.run(
        ["git", "-C", str(prepared_source), "config", "--unset", "core.autocrlf"],
        check=True,
    )
    config = tmp_path / "fixture-gitconfig"
    config.write_text("[core]\n    autocrlf = true\n")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv(
        "GIT_CONFIG_GLOBAL", str(config) if scope == "global" else os.devnull
    )
    monkeypatch.setenv(
        "GIT_CONFIG_SYSTEM", str(config) if scope == "system" else os.devnull
    )
    for name in ("changed.txt", "codex-rs/Cargo.lock"):
        path = prepared_source / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    build._verify_prepared_source(prepared_source, recipe / prepare.PATCH)


def test_every_companion_is_required_and_independently_arch_checked(captured, recipe):
    target, candidate, _ = captured
    for name in build.TARGETS[target]["executables"]:
        path = candidate / name
        original = path.read_bytes()
        path.unlink()
        with pytest.raises(ValueError):
            build.verify_build(target, candidate, root=recipe)
        image = bytearray(original)
        struct.pack_into(
            "<H",
            image,
            18 if target == "linux-x64" else 68,
            183 if target == "linux-x64" else 0xAA64,
        )
        path.write_bytes(image)
        rehash(candidate, name)
        with pytest.raises(ValueError):
            build.verify_build(target, candidate, root=recipe)
        path.write_bytes(original)
        rehash(candidate, name)
    build.verify_build(target, candidate, root=recipe)


@pytest.mark.parametrize(
    "change", ["patched", "unrelated", "untracked", "ignored", "lock", "revision"]
)
def test_source_capture_rejects_changes_beyond_preparation(
    prepared_source, recipe, monkeypatch, change
):
    if change == "revision":
        monkeypatch.setattr(prepare, "UPSTREAM_REVISION", "0" * 40)
    elif change == "ignored":
        (prepared_source / ".git/info/exclude").write_text("ignored.txt\n")
        (prepared_source / "ignored.txt").write_text("injected")
    else:
        name = {
            "patched": "changed.txt",
            "unrelated": "LICENSE",
            "untracked": "extra.txt",
            "lock": "codex-rs/Cargo.lock",
        }[change]
        (prepared_source / name).write_text("unexpected source edit")
    with pytest.raises(ValueError, match="source"):
        build._verify_prepared_source(prepared_source, recipe / prepare.PATCH)


def test_missing_embedded_bwrap_digest_even_with_updated_binary_hash(factory, recipe):
    candidate, _ = factory("linux-x64")
    (candidate / "bin/codex").write_bytes(elf(b"no bwrap digest"))
    rehash(candidate, "bin/codex")
    with pytest.raises(ValueError, match="embed"):
        build.verify_build("linux-x64", candidate, root=recipe)


def test_duplicate_json_key_rejected(captured, recipe):
    target, candidate, _ = captured
    path = candidate / build.RECEIPT
    path.write_text(
        path.read_text().replace(
            '"proof_status": "not-run"',
            '"proof_status": "not-run", "proof_status": "not-run"',
        )
    )
    with pytest.raises(ValueError, match="Duplicate"):
        build.verify_build(target, candidate, root=recipe)


def test_cli_is_available_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-S", str(Path(build.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "build-key" in result.stdout and "verify-build" in result.stdout
    assert "snapshot-build" in result.stdout
