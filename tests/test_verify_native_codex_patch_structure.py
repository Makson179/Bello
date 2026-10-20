"""Hand-edited native helper patches must stay structurally valid.

The Rust sources are not available where these patches are edited, so a wrong
hunk count would otherwise appear only in a native build. These checks use
synthetic diffs plus the shipped patches; they never compile or apply anything.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

from scripts import verify_native_codex_patch_structure as structure


ROOT = Path(__file__).resolve().parents[1]
PATCHES = ("scripts/native-codex-0.161.0.patch", "scripts/native-codex-selection.patch")

MODIFIED = textwrap.dedent("""\
    diff --git a/src/lib.rs b/src/lib.rs
    index 1111111..2222222 100644
    --- a/src/lib.rs
    +++ b/src/lib.rs
    @@ -10,3 +10,4 @@ fn first() {
     keep
    +added
     keep
     keep
    @@ -40,4 +41,3 @@ fn second() {
     keep
    -removed
     keep
     keep
    """)

NEW_FILE = textwrap.dedent("""\
    diff --git a/src/new.rs b/src/new.rs
    new file mode 100644
    index 0000000..3333333
    --- /dev/null
    +++ b/src/new.rs
    @@ -0,0 +1,2 @@
    +first
    +second
    """)


def problems(text: str) -> list[str]:
    return structure.check_text(text)


@pytest.mark.parametrize("name", PATCHES)
def test_shipped_patches_are_structurally_valid(name):
    assert structure.check(ROOT / name) == []


@pytest.mark.parametrize("name", PATCHES)
def test_git_parses_the_shipped_patches_and_rejects_a_miscounted_hunk(name, tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def parse(path):
        return subprocess.run([git, "apply", "--numstat", str(path)], cwd=tmp_path, env=environment,
                              capture_output=True, text=True)

    assert parse(ROOT / name).returncode == 0
    broken = tmp_path / "broken.patch"
    broken.write_text((NEW_FILE.replace("+1,2", "+1,3")), encoding="utf-8")
    result = parse(broken)
    assert result.returncode != 0 and "corrupt patch" in result.stderr
    assert problems(NEW_FILE.replace("+1,2", "+1,3"))  # the structural check agrees with git


def test_shipped_native_patch_covers_the_context_and_async_sources():
    files, errors = structure.parse((ROOT / PATCHES[0]).read_text(encoding="utf-8"))
    paths = {entry.path_b for entry in files}
    assert errors == [] and len(paths) == len(files)
    assert {"codex-rs/core/src/bello_compact.rs", "codex-rs/core/src/session/context_window.rs",
            "codex-rs/core/src/tools/native_async.rs"} <= paths


def test_valid_modified_and_new_file_sections_pass():
    assert problems(MODIFIED + NEW_FILE) == []


def test_preamble_bare_context_blank_and_no_newline_marker_are_accepted():
    text = "License text\nnot a diff line\n" + MODIFIED.replace(" keep\n+added", " keep\n\n+added", 1).replace(
        "@@ -10,3 +10,4 @@", "@@ -10,4 +10,5 @@", 1).replace("-removed\n keep\n keep\n", "-removed\n keep\n keep\n\\ No newline at end of file\n")
    assert problems(text) == []


@pytest.mark.parametrize("old,new,expected", [
    ("@@ -10,3 +10,4 @@", "@@ -10,4 +10,4 @@", "more lines|declares"),
    ("@@ -10,3 +10,4 @@", "@@ -10,3 +10,5 @@", "declares more|truncated|more lines"),
    ("@@ -10,3 +10,4 @@", "@@ -10,2 +10,3 @@", "declared counts too small|text after hunk"),
])
def test_wrong_hunk_counts_are_rejected(old, new, expected):
    result = problems(MODIFIED.replace(old, new, 1))
    assert result and any(any(word in item for word in expected.split("|")) for item in result)


def test_stale_new_side_offset_after_an_earlier_hunk_is_rejected():
    result = problems(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -40,4 +40,3 @@"))
    assert len(result) == 1 and "starts at new line 40" in result[0]


def test_overlapping_hunks_are_rejected():
    result = problems(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -11,4 +12,3 @@"))
    assert any("overlaps or precedes" in item for item in result)


@pytest.mark.parametrize("change,expected", [
    (("--- a/src/lib.rs", "--- a/src/other.rs"), "does not match"),
    (("+++ b/src/lib.rs", "+++ b/src/other.rs"), "does not match"),
    (("diff --git a/src/lib.rs b/src/lib.rs", "diff --git a/src/lib.rs b/src/other.rs"), "paths differ"),
])
def test_inconsistent_file_headers_are_rejected(change, expected):
    assert any(expected in item for item in problems(MODIFIED.replace(*change, 1)))


@pytest.mark.parametrize("edit,expected", [
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -0,0 +1,3 @@"), "more lines|declares more|truncated"),
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -0,0 +1,1 @@"), "declared counts too small|text after hunk"),
    (lambda text: text.replace("+second", " second"), "more lines|non-added"),
    (lambda text: text.replace("@@ -0,0 +1,2 @@", "@@ -1,0 +1,2 @@"), "exactly one"),
    (lambda text: text.replace("--- /dev/null", "--- a/src/new.rs"), "does not match"),
])
def test_malformed_new_files_are_rejected(edit, expected):
    result = problems(edit(NEW_FILE))
    assert result and any(any(word in item for word in expected.split("|")) for item in result)


def test_duplicate_file_sections_and_empty_patches_are_rejected():
    assert any("repeats" in item for item in problems(NEW_FILE + NEW_FILE))
    assert problems("just text\n") == ["the patch contains no 'diff --git' file sections"]


def test_command_line_reports_problems_and_succeeds_on_valid_patches(tmp_path, capsys):
    good, bad = tmp_path / "good.patch", tmp_path / "bad.patch"
    good.write_text(MODIFIED + NEW_FILE, encoding="utf-8")
    bad.write_text(MODIFIED.replace("@@ -40,4 +41,3 @@", "@@ -40,4 +40,3 @@"), encoding="utf-8")
    assert structure.main([str(good)]) == 0
    assert structure.main([str(bad)]) == 1
    assert "starts at new line 40" in capsys.readouterr().err


def test_crlf_checkouts_are_normalized_like_preparation(tmp_path):
    patch = tmp_path / "crlf.patch"
    patch.write_bytes((MODIFIED + NEW_FILE).replace("\n", "\r\n").encode("utf-8"))
    assert structure.check(patch) == []


# The native Rust cannot be compiled where the patch is edited. These source-text
# checks pin the plain-text summary contract so an edit cannot silently drop it.
def added_source(name: str) -> str:
    files, _ = structure.parse((ROOT / PATCHES[0]).read_text(encoding="utf-8"))
    section = next(entry for entry in files if entry.path_b == name)
    return "\n".join(line[1:] for hunk in section.hunks for line in hunk.body if line[:1] in "+ ")


def test_native_summary_contract_is_plain_text_with_the_agreed_limits():
    source = added_source("codex-rs/core/src/bello_compact.rs")
    window = added_source("codex-rs/core/src/session/context_window.rs")
    assert "const SUMMARY_TIMEOUT: Duration = Duration::from_secs(500);" in source
    assert "pub(crate) const BELLO_SUMMARY_TARGET_CHARS: usize = 16_000;" in window
    assert "pub(crate) const BELLO_SUMMARY_MAX_CHARS: usize = 48_000;" in window
    # One wall-clock deadline wraps both attempts; over-long text is rejected.
    assert source.count("tokio::time::timeout(") == 1 and "for _ in 0..2" in source
    assert "summary.chars().count() > BELLO_SUMMARY_MAX_CHARS" in source
    # No JSON schema, envelope or heading is requested from the summarizer.
    assert "output_schema" not in source
    assert "Return JSON" not in source and "status='complete'" not in source
    assert "Goal; Constraints" not in source
    # Headroom: the reserve holds a summary of the requested size, plus a safety margin.
    assert "(BELLO_SUMMARY_TARGET_CHARS / 4) as i64" in window
    assert "BELLO_SUMMARY_OUTPUT_RESERVE + BELLO_CONTEXT_SAFETY_MARGIN" in window
    assert "bello_input_budget(hard_limit)" in source and "(&turn, budget)" in source
    assert "hard_limit.saturating_sub" not in source
