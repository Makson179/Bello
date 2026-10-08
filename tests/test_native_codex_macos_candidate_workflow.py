"""stdlib-only structural contracts for the independently keyed Mac workflow."""
from pathlib import Path
import re

import pytest

from scripts import build_native_codex_macos_candidate as build

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/native-codex-macos-candidate.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def step(title):
    marker = "      - name: " + title + "\n"
    start = TEXT.index(marker)
    end = TEXT.find("\n      - ", start + len(marker))
    return TEXT[start:] if end < 0 else TEXT[start:end]


def test_only_authorized_branch_read_permissions_no_release_or_cancellation():
    assert "branches: ['codex/072-recovery-validation']" in TEXT
    assert TEXT.count("if: github.ref == 'refs/heads/codex/072-recovery-validation'") == 2
    assert "workflow_dispatch:" in TEXT and "paths:" not in TEXT
    assert "contents: read" in TEXT and "contents: write" not in TEXT
    assert "cancel-in-progress: false" in TEXT
    assert "secrets." not in TEXT and "gh release" not in TEXT
    assert "persist-credentials: true" not in TEXT
    assert TEXT.count("persist-credentials: false") == 3


def test_native_arm64_platform_and_exact_pins():
    assert TEXT.count("runs-on: macos-15\n") == 2
    assert TEXT.count('test "$(uname -m)" = arm64') == 2
    assert "ref: " + build.source_candidate.UPSTREAM_REVISION in TEXT
    assert "toolchain: '1.95.0'" in TEXT
    assert "rusty-v8-v150.4.0" in TEXT
    for name in build.V8_HASHES:
        assert name in TEXT
    assert "build_native_codex_macos_candidate.py verify-v8" in TEXT
    assert "--bin codex --bin codex-code-mode-host" in TEXT
    assert "strip " not in TEXT and "codesign --force" not in TEXT


def test_ready_cache_never_has_fallback_but_cargo_is_target_scoped():
    blocks = re.split(r"\n      - ", TEXT)
    restores = [block for block in blocks if block.startswith("uses: actions/cache/restore@v4")]
    assert len(restores) == 2
    ready = next(block for block in restores if "id: candidate" in block)
    cargo = next(block for block in restores if "id: cargo_cache" in block)
    assert "ready-v1-${{ steps.identity.outputs.key }}" in ready and "restore-keys" not in ready
    assert "restore-keys: native-codex-candidate-0161-darwin-arm64-cargo-v1-" in cargo
    assert "native-codex-candidate-0161-linux" not in TEXT
    assert "native-codex-candidate-0161-windows" not in TEXT


@pytest.mark.parametrize("title,command", [
    ("Test actual native path URI conversions", "-p codex-utils-path-uri --lib"),
    ("Test actual lossless permission path serialization", "-p codex-protocol --lib bello_permission_path_roundtrip"),
])
def test_actual_rust_regressions_mandatory_before_cache_and_snapshot(title, command):
    block = step(title)
    assert "cargo test --locked --release --target aarch64-apple-darwin " + command in block
    assert "if: steps.candidate.outputs.cache-hit != 'true'" in block
    assert "continue-on-error" not in block and "||" not in block
    assert TEXT.index(title) < TEXT.index("Preserve successful recompilable Cargo state")
    assert TEXT.index(title) < TEXT.index("Capture signed Mach-O")


def test_native_format_receipt_and_signature_check_precede_proof_execution():
    assert "--restore-executable-modes" in step("Verify every byte and native signature before executing candidate")
    assert TEXT.index("Verify every byte and native signature before executing candidate") < TEXT.index("Install CPU inference")
    assert TEXT.index("Preserve binaries independently") < TEXT.index("  macos-proof:")
    assert "needs: macos-build" in TEXT
    assert "--modernbert" in step("Prove selection9 async14 history1 and CPU inference4 plus bypass2")


def test_qualification_package_install_upload_order_and_scope():
    steps = ["Prove selection9", "Package only the qualified", "Prove the real fresh-cache",
             "Preserve qualified archive"]
    assert [TEXT.index(name) for name in steps] == sorted(TEXT.index(name) for name in steps)
    install = step("Prove the real fresh-cache installer and installed native CPU pipeline")
    assert "install-local --target darwin-arm64" in install and "--modernbert" in install
    assert '--output "$RUNNER_TEMP/bello-0161-macos-installed-proof"' in install
    assert '--artifact "$RUNNER_TEMP/bello-0161-macos-release"' in install
    assert '--output "$RUNNER_TEMP/bello-0161-macos-release"' in step("Package only the qualified exact candidate with all installable hashes")
    assert "continue-on-error" not in install
    bounded = step("Preserve bounded synthetic proof receipts even on failure")
    assert "if: always()" in bounded and "installed-proof/installed-proof.json" in bounded
    assert "guardian-control" not in bounded and "empty-home" not in bounded and "public-model-cache" not in bounded
    assert "candidate-proof/worker/*/*/result.json" in bounded


def test_only_optional_cache_storage_can_continue_after_failure():
    for block in re.split(r"\n      - ", TEXT):
        if "continue-on-error:" in block:
            assert "uses: actions/cache/save@v4" in block
