"""Small, dependency-free validation and export for local distiller bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re


FORMAT = "bello.log-distiller.v1"
ARCHITECTURE = "modernbert-token-mlp-v1"
MAX_LENGTH, OVERLAP = 8192, 256
RENDERER = "original-excerpts-newline-v2"
FRESH_R80_CUTOFF = -0.47521790862083435
REQUIRED_ASSETS = {"config.json", "tokenizer.json", "tokenizer_config.json"}
OPTIONAL_ASSETS = {"special_tokens_map.json"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_bundle(model_path: str | Path) -> dict:
    """Check small metadata and file presence; never load or hash model weights.

    The worker verifies every checksum and tensor at first use. This lightweight
    check is appropriate for setup and preflight and imports no ML dependencies.
    """
    directory = Path(model_path).expanduser().absolute()
    path = directory / "manifest.json"
    if path.stat().st_size > 64 * 1024:
        raise ValueError("manifest_too_large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("architecture") != ARCHITECTURE):
        raise ValueError("unsupported_bundle")
    recipe = manifest.get("recipe")
    if (not isinstance(recipe, dict) or recipe.get("max_length") != MAX_LENGTH
            or recipe.get("overlap") != OVERLAP or recipe.get("renderer") != RENDERER
            or type(recipe.get("cutoff")) not in (int, float)
            or not math.isfinite(recipe["cutoff"])):
        raise ValueError("unsupported_inference_recipe")
    checkpoint = manifest.get("checkpoint")
    if (not isinstance(checkpoint, dict) or checkpoint.get("format") not in ("torch", "safetensors")
            or not isinstance(checkpoint.get("file"), str)
            or Path(checkpoint["file"]).name != checkpoint["file"]
            or "/" in checkpoint["file"] or "\\" in checkpoint["file"]
            or checkpoint["file"] in ("", ".", "..", "manifest.json")):
        raise ValueError("invalid_checkpoint_entry")
    pins = manifest.get("assets_sha256")
    if (not isinstance(pins, dict) or not REQUIRED_ASSETS <= set(pins)
            or not set(pins) <= REQUIRED_ASSETS | OPTIONAL_ASSETS
            or checkpoint["file"] in pins):
        raise ValueError("invalid_asset_manifest")
    for name, digest in {**pins, checkpoint["file"]: checkpoint.get("sha256")}.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid_asset_digest")
        if not (directory / name).is_file():
            raise ValueError("missing_bundle_asset")
    # These files can silently affect AutoTokenizer loading when present.
    for name in ("added_tokens.json", "special_tokens_map.json", "tokenizer.model", "chat_template.jinja"):
        if (directory / name).exists() and name not in pins:
            raise ValueError("unpinned_tokenizer_asset")
    config_path = directory / "config.json"
    if config_path.stat().st_size > 64 * 1024:
        raise ValueError("model_config_too_large")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if (not isinstance(config, dict) or config.get("model_type") != "modernbert"
            or config.get("hidden_size") != 768 or config.get("num_hidden_layers") != 22):
        raise ValueError("unsupported_base_architecture")
    return manifest


def export_bundle(checkpoint: str | Path, assets: str | Path, output: str | Path,
                  *, cutoff: float = FRESH_R80_CUTOFF) -> Path:
    """Create a new bundle of symlinks, keeping the large weights at their source.

    Source paths must continue to exist. The destination must not already exist.
    Checkpoints are hashed but never deserialized by this export operation.
    """
    checkpoint, assets, output = Path(checkpoint).resolve(), Path(assets).resolve(), Path(output).absolute()
    if type(cutoff) not in (int, float) or not math.isfinite(cutoff):
        raise ValueError("finite_cutoff_required")
    if not checkpoint.is_file() or not assets.is_dir():
        raise ValueError("missing_model_sources")
    names = REQUIRED_ASSETS | {name for name in OPTIONAL_ASSETS if (assets / name).is_file()}
    pins = {name: sha256(assets / name) for name in sorted(names)}
    checkpoint_format = "safetensors" if checkpoint.suffix == ".safetensors" else "torch"
    checkpoint_name = "checkpoint.safetensors" if checkpoint_format == "safetensors" else "checkpoint.pt"
    manifest = {
        "format": FORMAT, "architecture": ARCHITECTURE,
        "checkpoint": {"file": checkpoint_name, "format": checkpoint_format, "sha256": sha256(checkpoint)},
        "assets_sha256": pins,
        "recipe": {"max_length": MAX_LENGTH, "overlap": OVERLAP, "renderer": RENDERER, "cutoff": cutoff},
    }
    output.mkdir(parents=False, exist_ok=False)
    for name in names:
        (output / name).symlink_to(assets / name)
    (output / checkpoint_name).symlink_to(checkpoint)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    validate_bundle(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cutoff", type=float, default=FRESH_R80_CUTOFF)
    args = parser.parse_args(argv)
    print(export_bundle(args.checkpoint, args.assets, args.output, cutoff=args.cutoff))


if __name__ == "__main__":
    main()
