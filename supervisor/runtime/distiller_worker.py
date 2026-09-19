"""Offline CPU inference for portable ModernBERT token-MLP log bundles.

Bundle layout: manifest.json, checkpoint.pt (or checkpoint.safetensors), and
config.json/tokenizer.json/tokenizer_config.json in the same directory. A
special_tokens_map.json may also be pinned. The JSON manifest has the form::

    {"format": "bello.log-distiller.v1", "architecture": "modernbert-token-mlp-v1",
     "checkpoint": {"file": "checkpoint.pt", "format": "torch", "sha256": "..."},
     "assets_sha256": {"config.json": "...", "tokenizer.json": "...",
                       "tokenizer_config.json": "..."},
     "recipe": {"max_length": 8192, "overlap": 256,
                "renderer": "original-excerpts-newline-v2",
                "cutoff": -0.47521790862083435}}

Torch bundles retain the native fresh checkpoint dictionary and metadata.
Safetensors bundles contain its model state dict. Neither format loads Python
code from the bundle. Different compatible weights/cutoffs use a new bundle.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import math
import os
from pathlib import Path
import sys
import unicodedata

from supervisor.runtime.distiller_bundle import (
    ARCHITECTURE, FORMAT, FRESH_R80_CUTOFF, MAX_LENGTH, OPTIONAL_ASSETS,
    OVERLAP, RENDERER, REQUIRED_ASSETS, sha256, validate_bundle,
)

PARAMETERS, HEAD_PARAMETERS = 149211393, 197121
MODEL_ID = "answerdotai/ModernBERT-base"
REVISION = "8949b909ec900327062f0ebf497f51aef5e6f0c8"
MAX_REQUEST_BYTES = 16 * 1024 * 1024 * 6 + 4096


def read_manifest(directory: Path) -> dict:
    """Validate the complete local recipe and pins before importing ML code."""
    manifest = validate_bundle(directory)
    checkpoint, pins = manifest["checkpoint"], manifest["assets_sha256"]
    files = {**pins, checkpoint["file"]: checkpoint.get("sha256")}
    for name, digest in files.items():
        if sha256(directory / name) != digest:
            raise ValueError("asset_checksum_mismatch")
    return manifest


def merge_spans(spans, length):
    result = []
    for a, b in sorted(spans):
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= length:
            raise ValueError("invalid_unicode_span")
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([a, b])
    return result


def render(text, spans):
    """Exact original excerpts and the frozen one-newline gap separator."""
    pieces = []
    for a, b in merge_spans(spans, len(text)):
        fragment = text[a:b]
        if pieces and not (pieces[-1].endswith(("\r", "\n")) or fragment.startswith(("\r", "\n"))):
            pieces.append("\n")
        pieces.append(fragment)
    return "".join(pieces)


def token_spans(text, offsets):
    """Frozen NFC/gap expansion, without training labels or data dependencies."""
    if not offsets:
        if text:
            raise ValueError("nonempty_source_without_tokens")
        return []
    segments, start = [], 0
    for i in range(1, len(text)):
        if unicodedata.category(text[i]).startswith("M"):
            continue
        before = text[start:i]
        if unicodedata.normalize("NFC", before + text[i]) != unicodedata.normalize("NFC", before) + unicodedata.normalize("NFC", text[i]):
            continue
        segments.append((start, i))
        start = i
    segments.append((start, len(text)))
    starts, ends = [a for a, _ in segments], [b for _, b in segments]
    masks = []
    for a, b in offsets:
        if not 0 <= a < b <= len(text):
            raise ValueError("invalid_native_token_offset")
        masks.append([starts[bisect.bisect_right(starts, a) - 1], ends[bisect.bisect_left(ends, b)]])
    order = sorted(range(len(masks)), key=lambda i: masks[i])
    first = order[0]
    masks[first][0] = 0
    furthest, covered = first, masks[first][1]
    for i in order[1:]:
        a, b = masks[i]
        if a > covered:
            masks[furthest][1] = a
        if b > covered:
            furthest, covered = i, b
    if covered < len(text):
        masks[furthest][1] = len(text)
    return masks


def make_entry(text, focus, command, tokenizer, *, max_length=MAX_LENGTH, overlap=OVERLAP):
    """Full conditioning and original log tokens; overlap uses first ownership."""
    raw = tokenizer(text, add_special_tokens=False, truncation=False,
                    padding=False, return_offsets_mapping=True)
    ids, offsets = raw["input_ids"], [list(x) for x in raw["offset_mapping"]]
    spans = token_spans(text, offsets)
    if not ids:
        return [], spans
    prefix = f"Focus: {focus}\nCommand: {command}\n"
    prefix_ids = tokenizer(prefix, add_special_tokens=False, truncation=False)["input_ids"]
    capacity = max_length - len(prefix_ids) - tokenizer.num_special_tokens_to_add(pair=True)
    if capacity <= overlap:
        raise ValueError("conditioning_exceeds_window_capacity")
    encoded = tokenizer(prefix, text, truncation="only_second", padding=False,
                        max_length=max_length, stride=overlap, return_overflowing_tokens=True,
                        return_offsets_mapping=True)
    windows, owned, begin = [], set(), 0
    for wi, joint_ids in enumerate(encoded["input_ids"]):
        seq = encoded.sequence_ids(wi)
        native = [list(x) for x in encoded["offset_mapping"][wi]]
        positions = [p for p, s in enumerate(seq) if s == 1]
        count = len(positions)
        if ([joint_ids[p] for p in positions] != ids[begin:begin + count]
                or [native[p] for p in positions] != offsets[begin:begin + count]):
            raise ValueError("pair_tokenization_alignment_changed")
        owners = []
        for offset, position in enumerate(positions):
            index = begin + offset
            if index not in owned:
                owners.append((position, index))
                owned.add(index)
        if owners:
            windows.append({"input_ids": list(joint_ids), "owners": owners})
        begin += count - overlap
    if [index for window in windows for _, index in window["owners"]] != list(range(len(ids))):
        raise ValueError("missing_duplicate_or_reordered_prediction_ownership")
    return windows, spans


def validate_checkpoint_metadata(saved, manifest):
    if not isinstance(saved, dict):
        raise ValueError("checkpoint_metadata")
    signature = saved.get("signature", {})
    if (not isinstance(signature, dict) or saved.get("base_model") != MODEL_ID
            or saved.get("revision") != REVISION
            or saved.get("architecture") != "768-256-GELU-dropout0.1-1"
            or saved.get("resumable") is not False
            or signature.get("model_id") != MODEL_ID
            or signature.get("revision") != REVISION
            or signature.get("architecture") != saved["architecture"]
            or signature.get("tokenizer_sha256") != manifest["assets_sha256"]["tokenizer.json"]
            or signature.get("renderer") != RENDERER
            or signature.get("max_length") != MAX_LENGTH or signature.get("overlap") != OVERLAP):
        raise ValueError("checkpoint_metadata")
    cutoff = saved.get("threshold_logit")
    objective = saved.get("objective")
    if (type(cutoff) not in (int, float) or not math.isfinite(cutoff)
            or not isinstance(objective, dict) or objective.get("threshold_logit") != cutoff):
        raise ValueError("checkpoint_threshold_metadata")


class CPUSelector:
    def __init__(self, model, tokenizer, torch, cutoff):
        self.model, self.tokenizer, self.torch, self.cutoff = model, tokenizer, torch, cutoff

    def select(self, text, focus, command):
        if not text:
            return text
        windows, spans = make_entry(text, focus, command, self.tokenizer)
        scores = [None] * len(spans)
        with self.torch.inference_mode():
            for window in windows:
                ids = self.torch.tensor([window["input_ids"]], dtype=self.torch.long, device="cpu")
                mask = self.torch.ones_like(ids)
                values = self.model(ids, mask)[0].float().cpu().tolist()
                if len(values) != len(window["input_ids"]):
                    raise ValueError("invalid_model_shape")
                for position, index in window["owners"]:
                    if scores[index] is not None or not math.isfinite(values[position]):
                        raise ValueError("invalid_or_duplicate_model_scores")
                    scores[index] = values[position]
        if any(value is None for value in scores):
            raise ValueError("incomplete_model_scores")
        return render(text, [span for span, score in zip(spans, scores) if score >= self.cutoff])


def load_selector(directory: Path) -> CPUSelector:
    manifest = read_manifest(directory)
    # These imports exist only in the subprocess; the normal runtime stays small.
    import torch
    from transformers import AutoConfig, AutoModel, AutoTokenizer

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    checkpoint = manifest["checkpoint"]
    if checkpoint["format"] == "torch":
        saved = torch.load(directory / checkpoint["file"], map_location="cpu", weights_only=True, mmap=True)
        validate_checkpoint_metadata(saved, manifest)
        tensors = saved.get("model")
    else:
        from safetensors.torch import load_file
        tensors = load_file(str(directory / checkpoint["file"]), device="cpu")
    if (not isinstance(tensors, dict) or not tensors
            or any(not isinstance(value, torch.Tensor) for value in tensors.values())
            or sum(value.numel() for value in tensors.values()) != PARAMETERS
            or any(value.dtype != torch.float32 or not bool(torch.isfinite(value).all()) for value in tensors.values())):
        raise ValueError("checkpoint_tensors")
    config = AutoConfig.from_pretrained(str(directory), local_files_only=True, trust_remote_code=False)
    if (config.model_type != "modernbert" or config.hidden_size != 768 or config.num_hidden_layers != 22):
        raise ValueError("unsupported_base_architecture")
    config.reference_compile = False
    config.use_cache = False
    encoder = AutoModel.from_config(config, torch_dtype=torch.float32,
                                   attn_implementation="sdpa", trust_remote_code=False)

    class TokenModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = encoder
            self.head = torch.nn.Sequential(torch.nn.Linear(768, 256), torch.nn.GELU(),
                                            torch.nn.Dropout(.1), torch.nn.Linear(256, 1)).float()

        def forward(self, ids, mask):
            hidden = self.encoder(input_ids=ids, attention_mask=mask).last_hidden_state
            return self.head(hidden.float()).squeeze(-1)

    model = TokenModel()
    model.load_state_dict(tensors, strict=True, assign=True)
    model = model.to("cpu").requires_grad_(False).eval()
    if (sum(p.numel() for p in model.parameters()) != PARAMETERS
            or sum(p.numel() for p in model.head.parameters()) != HEAD_PARAMETERS):
        raise ValueError("model_parameter_count")
    tokenizer = AutoTokenizer.from_pretrained(str(directory), local_files_only=True,
                                              use_fast=True, trust_remote_code=False)
    if not tokenizer.is_fast or tokenizer.pad_token_id is None:
        raise ValueError("offset_capable_tokenizer_with_padding_required")
    return CPUSelector(model, tokenizer, torch, float(manifest["recipe"]["cutoff"]))


def emit(value):
    # The parent reads JSON bytes, independent of the platform's text encoding.
    # Windows pipes can otherwise encode output as cp1252 or reject Unicode.
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            selector = load_selector(args.model_path)
    except Exception:
        emit({"error": "model_bundle_unavailable"})
        return 1
    emit({"ready": True})
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return 1
        request = None
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or type(request.get("id")) is not int
                    or any(not isinstance(request.get(key), str) for key in ("log", "focus", "command"))):
                raise ValueError("request_schema")
            with contextlib.redirect_stdout(sys.stderr):
                text = selector.select(request["log"], request["focus"], request["command"])
            emit({"id": request["id"], "ok": True, "text": text})
        except Exception:
            emit({"id": request.get("id") if isinstance(request, dict) else None, "ok": False})


if __name__ == "__main__":
    raise SystemExit(main())
