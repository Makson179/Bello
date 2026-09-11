from __future__ import annotations

import base64
import os

import pytest

from supervisor.runtime.file_worker import operate, frame_response, parse_response


def test_framed_worker_roundtrip_cannot_confuse_content_with_protocol():
    nonce = "f" * 32
    value = {"text": "\x1eBELLO_FILE_V1:" + nonce + ":read_file:\nПривет\r\n"}
    frame = frame_response(nonce, "read_file", value)
    assert frame.isascii()
    result, diagnostics = parse_response("startup warning {not JSON}\n" + frame + "\r\n", nonce, "read_file")
    assert result == value and diagnostics == "startup warning {not JSON}"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "truncated", "bad-json", "wrong-order"])
def test_corrupt_frames_are_not_accepted(mutation):
    nonce = "a" * 32
    frame = frame_response(nonce, "read_file", {"text": "ok"})
    start, body = frame.split('{', 1)
    variants = {"missing": '{"text": "ok"}', "duplicate": frame + frame,
                "truncated": frame[:-4], "bad-json": frame.replace('{', '{bad', 1),
                "wrong-order": body[body.index('\x1f'):] + start + '{"text":"ok"}'}
    with pytest.raises(ValueError):
        parse_response(variants[mutation], nonce, "read_file")


def test_real_worker_emits_one_identified_frame(tmp_path):
    import base64
    import json
    import subprocess
    import sys
    from supervisor.runtime import file_worker

    path = tmp_path / "unicode.txt"
    path.write_text("Строка\n", encoding="utf-8")
    nonce = "a1" * 16
    request = {"name": "read_file", "arguments": {"path": str(path)}, "response_nonce": nonce}
    completed = subprocess.run([sys.executable, "-I", file_worker.__file__,
                                base64.b64encode(json.dumps(request).encode()).decode()],
                               capture_output=True, text=True, timeout=15, check=True)
    value, diagnostics = parse_response(completed.stdout, nonce, "read_file")
    assert value["text"] == "1: Строка\n" and diagnostics == ""
    request["arguments"]["path"] = str(tmp_path / "missing.txt")
    failed = subprocess.run([sys.executable, "-I", file_worker.__file__,
                             base64.b64encode(json.dumps(request).encode()).decode()],
                            capture_output=True, text=True, timeout=15)
    assert failed.returncode == 1
    error, diagnostics = parse_response(failed.stdout, nonce, "read_file")
    assert set(error) == {"error"} and "missing.txt" in error["error"]
    assert diagnostics == ""


def test_read_file_preserves_unicode_and_reports_exact_line_slice(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("first\nВторая\nthird\n", encoding="utf-8")
    result = operate("read_file", {"path": str(path), "offset": 2, "limit": 1})
    assert result == {"text": "2: Вторая\n", "offset": 2, "returned_lines": 1, "total_lines": 3}


def test_ambiguous_edit_changes_nothing(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("repeat repeat")
    with pytest.raises(ValueError, match="ambiguous|2 matches"):
        operate("edit_file", {"path": str(path), "old_text": "repeat", "new_text": "x"})
    assert path.read_text() == "repeat repeat"
    assert list(tmp_path.iterdir()) == [path]


def test_exact_edit_preserves_permissions_and_cleans_temporary_file(tmp_path):
    path = tmp_path / "executable.sh"
    path.write_text("one two")
    path.chmod(0o755)
    operate("edit_file", {"path": str(path), "old_text": "two", "new_text": "three"})
    assert path.read_text() == "one three"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o755
    assert list(tmp_path.iterdir()) == [path]


def test_symlink_write_is_not_followed_by_helper(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserved")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is not permitted")
    with pytest.raises(ValueError, match="symbolic"):
        operate("write_file", {"path": str(link), "content": "bad"})
    assert target.read_text() == "preserved"


def test_search_keeps_matching_lines_and_reports_read_errors(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("first\nneedle\nlast")
    (tmp_path / "binary").write_bytes(b"\xff\xfe\x00")
    result = operate("search", {"path": str(tmp_path), "pattern": "needle"})
    assert result == {"matches": [{"path": str(path), "line": 2, "text": "needle"}], "errors": []}


def test_image_is_returned_as_original_bytes_not_text_summary(tmp_path):
    path = tmp_path / "image.png"
    raw = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
    path.write_bytes(raw)
    result = operate("view_image", {"path": str(path)})
    assert result["mimeType"] == "image/png"
    assert base64.b64decode(result["data"]) == raw
    assert result["size"] == len(raw)


def test_html_disguised_as_image_is_rejected(tmp_path):
    path = tmp_path / "fake.png"
    path.write_text("<script>not an image</script>")
    with pytest.raises(ValueError, match="unsupported image"):
        operate("view_image", {"path": str(path)})
