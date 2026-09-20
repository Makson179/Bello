"""Public bundle/cache tests using tiny local assets, never the network or ML."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime import distiller_download as download
from supervisor.runtime.distiller_bundle import export_bundle, sha256


@pytest.fixture
def published(tmp_path, monkeypatch):
    checkpoint = tmp_path / "weights.safetensors"
    checkpoint.write_bytes(b"synthetic model")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "config.json").write_text(json.dumps({
        "model_type": "modernbert", "hidden_size": 768, "num_hidden_layers": 22,
    }))
    for name in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        (assets / name).write_text("{}")
    bundle = export_bundle(checkpoint, assets, tmp_path / "bundle")
    (bundle / "LICENSE").write_text("test license")
    (bundle / "NOTICE").write_text("test notice")
    monkeypatch.setattr(download, "MANIFEST_SHA256", sha256(bundle / "manifest.json"))
    monkeypatch.setattr(download, "default_bundle_path", lambda: bundle)
    return bundle


def hub_stub(monkeypatch, callback):
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = callback
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)


def test_complete_snapshot_never_contacts_hub(published, monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("cache hit must not request even remote metadata")
    hub_stub(monkeypatch, unexpected)
    assert download.ensure_default_bundle() == published


def test_incomplete_snapshot_downloads_only_pinned_public_files(published, monkeypatch):
    (published / "NOTICE").unlink()
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        (published / "NOTICE").write_text("test notice")
        return str(published)
    hub_stub(monkeypatch, fetch)
    assert download.ensure_default_bundle() == published
    assert len(calls) == 1
    assert calls[0] == {
        "repo_id": "Makson179/bello-log-distiller",
        "revision": "436bf8dceecb30d5494519d21175bc03e5c98795",
        "allow_patterns": list(download.MODEL_FILES),
        "cache_dir": str(published.parent.parent.parent),
        "endpoint": "https://huggingface.co", "token": False,
    }
    assert "README.md" not in calls[0]["allow_patterns"]
    assert download.ensure_default_bundle() == published
    assert len(calls) == 1


def test_download_must_finish_before_bundle_can_be_used(published, monkeypatch):
    (published / "checkpoint.safetensors").unlink()
    hub_stub(monkeypatch, lambda **kwargs: str(published))
    with pytest.raises(FileNotFoundError, match="incomplete"):
        download.ensure_default_bundle()


def test_reject_tampered_manifest_even_if_bundle_is_otherwise_valid(published, monkeypatch):
    path = published / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["recipe"]["cutoff"] = 123.0
    path.write_text(json.dumps(manifest))
    hub_stub(monkeypatch, lambda **kwargs: pytest.fail("do not silently replace a changed pin"))
    with pytest.raises(ValueError, match="checksum"):
        download.ensure_default_bundle()


def test_default_location_uses_hugging_face_cache(monkeypatch, tmp_path):
    constants = ModuleType("huggingface_hub.constants")
    constants.HF_HUB_CACHE = str(tmp_path / "custom-hf-cache")
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", constants)
    assert download.default_bundle_path() == (
        Path(constants.HF_HUB_CACHE) / "models--Makson179--bello-log-distiller"
        / "snapshots" / download.MODEL_REVISION)


def test_download_module_import_needs_no_optional_packages():
    result = subprocess.run([
        sys.executable, "-S", "-c",
        "import sys; import supervisor.runtime.distiller_download; "
        "assert not {'torch','transformers','huggingface_hub'} & sys.modules.keys()",
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def worker_stub(monkeypatch):
    class Worker:
        def __init__(self, path):
            self.path = path
        async def distill(self, text, focus, command):
            return text
        async def close(self):
            pass
    monkeypatch.setattr("supervisor.runtime.distiller.require_dependencies", lambda: None)
    monkeypatch.setattr("supervisor.runtime.distiller.LogDistiller", Worker)


@pytest.mark.asyncio
async def test_enabled_default_prepares_before_runtime_and_reuses_after_restart(
    tmp_path, published, worker_stub, monkeypatch,
):
    calls = []
    def prepare():
        calls.append("download")
        assert not client._started and client._journal is None
        return published
    monkeypatch.setattr(download, "ensure_default_bundle", prepare)
    client = RuntimeClient(cwd=tmp_path)
    client.configure_run(runtime_enabled=False, log_distiller={"enabled": True})
    assert calls == []  # Synchronous configuration does not block on networking.
    assert client._distiller_path == published
    await client.start()
    assert calls == ["download"] and client._distiller_auto
    old = client._distiller
    await client.stop()
    # stop leaves a closed journal object, which start replaces after preparation.
    client._journal = None
    await client.start()
    assert client._distiller is not old and client._distiller_auto
    assert calls == ["download", "download"]
    await client.stop()


@pytest.mark.asyncio
async def test_failed_first_download_does_not_start_runtime(tmp_path, published, worker_stub, monkeypatch):
    def failed():
        raise OSError("offline")
    monkeypatch.setattr(download, "ensure_default_bundle", failed)
    client = RuntimeClient(cwd=tmp_path)
    client.configure_run(log_distiller={"enabled": True})
    with pytest.raises(AppServerError, match="Cannot prepare.*--distiller-model"):
        await client.start()
    assert not client._started and client._journal is None and client._engines == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_local_override_or_disabled_never_downloads(tmp_path, published, worker_stub, monkeypatch, enabled):
    def unexpected():
        pytest.fail("explicit local path or off must never load the Hub")
    monkeypatch.setattr(download, "ensure_default_bundle", unexpected)
    monkeypatch.setattr(download, "default_bundle_path", unexpected)
    client = RuntimeClient(cwd=tmp_path)
    client.configure_run(log_distiller={"enabled": enabled, "model_path": str(published)})
    await client.start()
    assert not client._distiller_auto
    assert (client._distiller is not None) == enabled
    await client.stop()
