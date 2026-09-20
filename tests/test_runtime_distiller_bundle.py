"""Bundle and frozen text-recipe checks that never load ML dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from supervisor.runtime.distiller_bundle import export_bundle, validate_bundle
from supervisor.runtime.distiller_worker import make_entry, read_manifest, render, token_spans


def _sources(tmp_path: Path, suffix=".pt"):
    checkpoint = tmp_path / ("source" + suffix)
    checkpoint.write_bytes(b"synthetic checkpoint; deliberately not a serialized model\x00")
    assets = tmp_path / "assets"
    assets.mkdir()
    config = {"model_type": "modernbert", "hidden_size": 768, "num_hidden_layers": 22}
    (assets / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        (assets / name).write_text("{}\n", encoding="utf-8")
    return checkpoint, assets


@pytest.fixture
def bundle(tmp_path):
    checkpoint, assets = _sources(tmp_path)
    return export_bundle(checkpoint, assets, tmp_path / "bundle")


def _change_manifest(bundle, change):
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("suffix,filename,format_", [
    (".pt", "checkpoint.pt", "torch"),
    (".safetensors", "checkpoint.safetensors", "safetensors"),
])
def test_export_symlinks_sources_and_pins_metadata(tmp_path, suffix, filename, format_):
    checkpoint, assets = _sources(tmp_path, suffix)
    exported = export_bundle(checkpoint, assets, tmp_path / "bundle", cutoff=-0.75)
    manifest = read_manifest(exported)

    assert exported == tmp_path / "bundle"
    assert (exported / filename).is_symlink()
    assert (exported / filename).resolve() == checkpoint.resolve()
    assert manifest["checkpoint"] == {
        "file": filename,
        "format": format_,
        "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
    }
    assert manifest["recipe"] == {
        "max_length": 8192, "overlap": 256,
        "renderer": "original-excerpts-newline-v2", "cutoff": -0.75,
    }
    assert set(manifest["assets_sha256"]) == {
        "config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    }
    for name, digest in manifest["assets_sha256"].items():
        assert (exported / name).is_symlink()
        assert (exported / name).resolve() == (assets / name).resolve()
        assert digest == hashlib.sha256((assets / name).read_bytes()).hexdigest()


def test_preflight_does_not_hash_or_deserialize_checkpoint(bundle, monkeypatch):
    import supervisor.runtime.distiller_bundle as bundle_module

    def unexpected_hash(_path):
        pytest.fail("preflight must not read model weights")

    monkeypatch.setattr(bundle_module, "sha256", unexpected_hash)
    assert validate_bundle(bundle)["checkpoint"]["format"] == "torch"


def test_first_use_manifest_detects_changed_source(bundle):
    (bundle / "checkpoint.pt").resolve().write_bytes(b"changed synthetic bytes")
    validate_bundle(bundle)
    with pytest.raises(ValueError, match="asset_checksum_mismatch"):
        read_manifest(bundle)


@pytest.mark.parametrize("key,value", [
    ("max_length", 4096), ("overlap", 0), ("renderer", "summarize"),
    ("cutoff", True), ("cutoff", "-0.5"), ("cutoff", None),
    ("cutoff", float("nan")), ("cutoff", float("inf")),
])
def test_validate_rejects_malformed_recipe(bundle, key, value):
    _change_manifest(bundle, lambda manifest: manifest["recipe"].update({key: value}))
    with pytest.raises(ValueError, match="unsupported_inference_recipe"):
        validate_bundle(bundle)


@pytest.mark.parametrize("filename", [
    "../source.pt", "/tmp/source.pt", "subdir/checkpoint.pt", "subdir\\checkpoint.pt",
    "..", "manifest.json",
])
def test_validate_rejects_checkpoint_path_traversal(bundle, filename):
    _change_manifest(bundle, lambda manifest: manifest["checkpoint"].update(file=filename))
    with pytest.raises(ValueError, match="invalid_checkpoint_entry"):
        validate_bundle(bundle)


def test_validate_rejects_missing_tokenizer(bundle):
    (bundle / "tokenizer.json").unlink()
    with pytest.raises(ValueError, match="missing_bundle_asset"):
        validate_bundle(bundle)


def test_validate_rejects_required_tokenizer_without_pin(bundle):
    _change_manifest(bundle, lambda manifest: manifest["assets_sha256"].pop("tokenizer.json"))
    with pytest.raises(ValueError, match="invalid_asset_manifest"):
        validate_bundle(bundle)


@pytest.mark.parametrize("filename", [
    "added_tokens.json", "special_tokens_map.json", "tokenizer.model", "chat_template.jinja",
])
def test_validate_rejects_unpinned_tokenizer_sidecars(bundle, filename):
    if filename == "special_tokens_map.json":
        _change_manifest(bundle, lambda manifest: manifest["assets_sha256"].pop(filename))
    else:
        (bundle / filename).write_text("untrusted tokenizer override", encoding="utf-8")
    with pytest.raises(ValueError, match="unpinned_tokenizer_asset"):
        validate_bundle(bundle)


def test_recipe_modules_import_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-S", "-c",
         "import sys; import supervisor.runtime.distiller_bundle; "
         "import supervisor.runtime.distiller_worker; "
         "assert not {'torch', 'transformers', 'safetensors'} & sys.modules.keys()"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_token_spans_expand_combining_characters_and_unassigned_gaps():
    text = "  e\u0301  X  "
    spans = token_spans(text, [(2, 3), (3, 4), (6, 7)])
    assert spans == [[0, 6], [2, 4], [6, 9]]
    assert render(text, [spans[0]]) == "  e\u0301  "
    assert render(text, [spans[1]]) == "e\u0301"
    assert render(text, spans) == text


def test_token_spans_preserve_nfc_composing_hangul_as_original_codepoints():
    text = "\u1100\u1161!"
    spans = token_spans(text, [(0, 1), (1, 2), (2, 3)])
    assert spans == [[0, 2], [0, 2], [2, 3]]
    assert render(text, [spans[1]]) == "\u1100\u1161"


@pytest.mark.parametrize("text,offsets,error", [
    ("x", [], "nonempty_source_without_tokens"),
    ("abc", [(0, 0)], "invalid_native_token_offset"),
    ("abc", [(-1, 1)], "invalid_native_token_offset"),
    ("abc", [(0, 4)], "invalid_native_token_offset"),
])
def test_token_spans_reject_invalid_offsets(text, offsets, error):
    with pytest.raises(ValueError, match=error):
        token_spans(text, offsets)


def test_empty_source_has_no_spans_or_rendered_text():
    assert token_spans("", []) == []
    assert render("", []) == ""


def test_render_sorts_merges_and_preserves_exact_unicode_source_slices():
    text = "🙂e\u0301 OMIT β"
    assert render(text, [(9, 10), (1, 3), (0, 2)]) == "🙂e\u0301\nβ"


@pytest.mark.parametrize("text,spans,expected", [
    ("a\n OMIT b", [(0, 2), (8, 9)], "a\nb"),
    ("a OMIT \r\nb", [(0, 1), (7, 10)], "a\r\nb"),
    ("a OMIT b", [(0, 1), (7, 8)], "a\nb"),
])
def test_render_adds_separator_only_when_excerpts_need_it(text, spans, expected):
    assert render(text, spans) == expected


class _Encoded(dict):
    def sequence_ids(self, window):
        return [None, 0, None] + [1] * (len(self["input_ids"][window]) - 4) + [None]


class _Tokenizer:
    """Two overlapping pair windows with known token IDs and source offsets."""

    def __init__(self):
        self.prefix = None

    def num_special_tokens_to_add(self, pair):
        assert pair is True
        return 3

    def __call__(self, first, second=None, **kwargs):
        if second is not None:
            self.prefix = first
            assert kwargs["truncation"] == "only_second"
            assert kwargs["max_length"] == 8
            assert kwargs["stride"] == 1
            return _Encoded(
                input_ids=[[100, 10, 101, 1, 2, 3, 4, 102], [100, 10, 101, 4, 5, 6, 102]],
                offset_mapping=[
                    [(0, 0), (0, len(first)), (0, 0), (0, 1), (1, 2), (2, 3), (3, 4), (0, 0)],
                    [(0, 0), (0, len(first)), (0, 0), (3, 4), (4, 5), (5, 6), (0, 0)],
                ],
            )
        assert kwargs["truncation"] is False
        if first == "abcdef":
            return {"input_ids": [1, 2, 3, 4, 5, 6], "offset_mapping": [(i, i + 1) for i in range(6)]}
        return {"input_ids": [10]}


def test_make_entry_keeps_full_conditioning_and_first_ownership_across_overlap():
    tokenizer = _Tokenizer()
    windows, spans = make_entry("abcdef", "find β failure", "run\n--verbose", tokenizer,
                                max_length=8, overlap=1)
    assert tokenizer.prefix == "Focus: find β failure\nCommand: run\n--verbose\n"
    assert spans == [[i, i + 1] for i in range(6)]
    assert windows[0]["owners"] == [(3, 0), (4, 1), (5, 2), (6, 3)]
    assert windows[1]["owners"] == [(4, 4), (5, 5)]
    assert [i for window in windows for _, i in window["owners"]] == list(range(6))
