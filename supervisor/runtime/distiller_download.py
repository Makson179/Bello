"""Download the published selector once; reuse its pinned local HF snapshot."""

from __future__ import annotations

import logging
from pathlib import Path

from supervisor.runtime.distiller_bundle import sha256, validate_bundle


logger = logging.getLogger(__name__)
MODEL_REPOSITORY = "Makson179/bello-log-distiller"
MODEL_REVISION = "436bf8dceecb30d5494519d21175bc03e5c98795"
MANIFEST_SHA256 = "ecbc6016e6b3e88890339f2c092187016d30bf6913a9b1cede100db2846aad91"
MODEL_FILES = (
    "checkpoint.safetensors", "config.json", "manifest.json",
    "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
    "LICENSE", "NOTICE",
)


def default_bundle_path() -> Path:
    # Lazy optional import: base Bello and distiller-off never need the Hub.
    from huggingface_hub.constants import HF_HUB_CACHE

    return (Path(HF_HUB_CACHE).expanduser().absolute()
            / "models--Makson179--bello-log-distiller" / "snapshots" / MODEL_REVISION)


def _check_snapshot(directory: Path) -> None:
    if not all((directory / name).is_file() for name in MODEL_FILES):
        raise FileNotFoundError("incomplete published log-distiller snapshot")
    manifest = directory / "manifest.json"
    if manifest.stat().st_size > 64 * 1024 or sha256(manifest) != MANIFEST_SHA256:
        raise ValueError("published log-distiller manifest checksum mismatch")
    # The manifest itself is release-pinned. All tensor/asset hashes are checked
    # by the isolated worker before inference, not by importing ML at setup.
    validate_bundle(directory)


def ensure_default_bundle() -> Path:
    """Ensure all published assets are cached before a run starts.

    Complete snapshots need no network, even for metadata. The Hub downloader
    owns atomic writes, resume and inter-process locks for concurrent first use.
    No logs, credentials or project contents are sent to the public repository.
    """
    directory = default_bundle_path()
    try:
        _check_snapshot(directory)
        return directory
    except FileNotFoundError:
        pass
    from huggingface_hub import snapshot_download

    logger.info("Downloading Bello log distiller (~599 MB) from %s at %s",
                MODEL_REPOSITORY, MODEL_REVISION)
    snapshot_download(
        repo_id=MODEL_REPOSITORY, revision=MODEL_REVISION,
        allow_patterns=list(MODEL_FILES),
        cache_dir=str(directory.parent.parent.parent),
        endpoint="https://huggingface.co", token=False,
    )
    _check_snapshot(directory)
    return directory


def main() -> None:
    """Optional prefetch: python -m supervisor.runtime.distiller_download."""
    print(ensure_default_bundle())


if __name__ == "__main__":
    main()
