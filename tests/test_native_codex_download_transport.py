"""Exercise the real native downloader with offline HTTP response fixtures."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import hashlib
import io
import json
import tarfile
from urllib.error import URLError

import pytest

from supervisor.runtime import native_codex_install as install


class Response:
    def __init__(self, chunks, *, url="https://release-assets.githubusercontent.com/fixture"):
        self.chunks = deque(chunks)
        self.url = url
        self.closed = False
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, size):
        assert size == 1024 * 1024
        self.reads += 1
        item = self.chunks.popleft() if self.chunks else b""
        if isinstance(item, BaseException):
            raise item
        assert len(item) <= size
        return item


@pytest.fixture
def release(tmp_path, monkeypatch):
    # Every test must replace urlopen explicitly; an accidental request fails
    # before the real network function can be reached.
    monkeypatch.setattr(install, "urlopen", lambda *a, **kw: pytest.fail("real network forbidden"))
    monkeypatch.delenv("BELLO_CODEX_BINARY", raising=False)
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    contents = {name: f"offline fixture {name}".encode() for name in install._FILES
                if name != "selection-manifest.json"}
    files = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    manifest = json.dumps({
        "binary_sha256": files["bin/codex"], "files": files,
        "feature": "bello_native_selection", "protocol": 1,
        "version": "0.153.4", "transport_timeout_seconds": 315,
    }, sort_keys=True).encode()
    contents["selection-manifest.json"] = manifest
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, value in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            archive.addfile(entry, io.BytesIO(value))
    payload = buffer.getvalue()
    bundle = install.NativeBundle(
        "https://github.com/Makson179/Bello/releases/download/fixture/native.tar.gz",
        hashlib.sha256(payload).hexdigest(), hashlib.sha256(manifest).hexdigest(),
    )
    monkeypatch.setattr(install, "BUNDLES", {("Darwin", "arm64"): bundle})
    return bundle, payload, tmp_path / "runtime" / "native-codex"


def serve(monkeypatch, bundle, response):
    requests = []

    def open_response(request, *, timeout):
        assert request.full_url == bundle.url
        assert request.get_header("User-agent") == "Bello-native-codex-installer"
        assert timeout == 60
        requests.append(request)
        return response

    monkeypatch.setattr(install, "urlopen", open_response)
    return requests


def assert_not_published(release):
    bundle, _, root = release
    assert not (root / bundle.archive_sha256).exists()
    assert not list(root.glob(".download-*"))
    # A reusable installation lock may remain, but no partial archive/bundle.
    assert {path.name for path in root.iterdir()} <= {f".{bundle.archive_sha256}.lock"}


def forbid_unpack(monkeypatch):
    monkeypatch.setattr(install, "_unpack", lambda *a: pytest.fail("unverified archive extracted"))


def test_real_download_accepts_exact_sha_and_secure_cdn_redirect(release, monkeypatch):
    bundle, payload, root = release
    response = Response([payload[:13], payload[13:51], payload[51:]])
    calls = serve(monkeypatch, bundle, response)
    # The exact boundary is allowed, not rejected by an off-by-one size check.
    monkeypatch.setattr(install, "_MAX_DOWNLOAD", len(payload))

    command, manifest = install.ensure_native_selection()

    assert command[0] == str(root / bundle.archive_sha256 / "bin" / "codex")
    assert manifest == root / bundle.archive_sha256 / "selection-manifest.json"
    install._verify(manifest.parent, bundle)
    assert response.closed and response.reads == 4 and len(calls) == 1
    assert not list(root.glob(".download-*"))
    assert install.ensure_native_selection() == (command, manifest)
    assert len(calls) == 1  # A verified cache hit is genuinely offline.


@pytest.mark.parametrize("corruption", ["changed", "truncated"])
def test_real_archive_sha_mismatch_never_extracts_or_publishes(release, monkeypatch, corruption):
    bundle, payload, _ = release
    bad = b"x" + payload[1:] if corruption == "changed" else payload[:-7]
    response = Response([bad])
    serve(monkeypatch, bundle, response)
    forbid_unpack(monkeypatch)

    with pytest.raises(ValueError, match="archive checksum mismatch"):
        install.ensure_native_selection()

    assert response.closed
    assert_not_published(release)


@pytest.mark.parametrize("url", ["http://release-assets.githubusercontent.com/fixture", "file:///fixture"])
def test_insecure_redirect_rejected_before_reading_payload(release, monkeypatch, url):
    bundle, payload, _ = release
    response = Response([payload], url=url)
    serve(monkeypatch, bundle, response)
    forbid_unpack(monkeypatch)

    with pytest.raises(ValueError, match="redirected to an insecure URL"):
        install.ensure_native_selection()

    assert response.closed and response.reads == 0
    assert_not_published(release)


def test_size_cap_counts_all_chunks_and_cleans_partial_download(release, monkeypatch):
    bundle, payload, _ = release
    response = Response([payload[:20], payload[20:]])
    serve(monkeypatch, bundle, response)
    monkeypatch.setattr(install, "_MAX_DOWNLOAD", len(payload) - 1)
    forbid_unpack(monkeypatch)

    with pytest.raises(ValueError, match="exceeds its size limit"):
        install.ensure_native_selection()

    assert response.closed and response.reads == 2
    assert_not_published(release)


@pytest.mark.parametrize("error_type", [ConnectionResetError, TimeoutError])
def test_midstream_transport_error_closes_and_removes_partial_download(release, monkeypatch, error_type):
    bundle, payload, _ = release
    response = Response([payload[:20], error_type("offline simulated transport failure")])
    serve(monkeypatch, bundle, response)
    forbid_unpack(monkeypatch)

    with pytest.raises(error_type, match="offline simulated transport failure"):
        install.ensure_native_selection()

    assert response.closed and response.reads == 2
    assert_not_published(release)


def test_connect_error_removes_staging_directory(release, monkeypatch):
    def unavailable(*args, **kwargs):
        raise URLError("offline simulated connect failure")

    monkeypatch.setattr(install, "urlopen", unavailable)
    forbid_unpack(monkeypatch)

    with pytest.raises(URLError, match="offline simulated connect failure"):
        install.ensure_native_selection()

    assert_not_published(release)


@pytest.mark.parametrize("url", [
    "http://github.com/Makson179/Bello/releases/download/fixture/native.tar.gz",
    "https://github.com/other/Bello/releases/download/fixture/native.tar.gz",
    "https://github.com.evil.example/Makson179/Bello/releases/download/fixture/native.tar.gz",
])
def test_unpinned_source_is_rejected_without_opening_network(release, monkeypatch, url):
    bundle, _, _ = release
    monkeypatch.setattr(install, "BUNDLES", {("Darwin", "arm64"): replace(bundle, url=url)})
    forbid_unpack(monkeypatch)

    with pytest.raises(ValueError, match="must use the pinned Bello release"):
        install.ensure_native_selection()

    assert_not_published(release)
