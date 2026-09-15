"""Pinned, private native Codex helper for log selection, not a global CLI update.

Only the native-Codex + distiller path calls this module. Downloads happen before
starting app-server and outside its RPC deadlines. Explicit host overrides remain
supported and go through the existing native capability validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import tarfile
import tempfile
from urllib.request import Request, urlopen

from supervisor.state import FileLock


@dataclass(frozen=True)
class NativeBundle:
    url: str
    archive_sha256: str
    manifest_sha256: str


# Add a platform only after the offline native provider-boundary proof passes.
BUNDLES: dict[tuple[str, str], NativeBundle] = {
    ("Darwin", "arm64"): NativeBundle(
        url=("https://github.com/Makson179/Bello/releases/download/"
             "native-codex-0.153.4-selection-v1/"
             "bello-native-codex-0.153.4-aarch64-apple-darwin.tar.gz"),
        archive_sha256="587bdeb27a9d6896252238805b2057e15b6d196cbec1411f76d5e2563e81ced2",
        manifest_sha256="cc16402a699eba247b6fe34fd7f71328d318f8d5a83ef836498d8dbadd0e136e",
    ),
}
_MAX_DOWNLOAD = 1024 * 1024 * 1024
_MAX_UNPACKED = 2 * _MAX_DOWNLOAD
_FILES = frozenset({
    "bin/codex", "bin/codex-code-mode-host", "selection-manifest.json",
    "LICENSE", "NOTICE", "native-codex-selection.patch",
    "THIRD-PARTY-NOTICES", "BUILD-INFO",
})


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _owned(path: Path, *, directory: bool = False, private: bool = False) -> None:
    metadata = path.lstat()
    valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if path.is_symlink() or not valid_type:
        raise ValueError(f"Native Codex cache path must be a real {'directory' if directory else 'file'}: {path}")
    if os.name != "nt" and (metadata.st_uid != os.getuid()
                            or stat.S_IMODE(metadata.st_mode) & (0o077 if private else 0o022)):
        raise ValueError(f"Native Codex cache path must be owned by this user and not writable by others: {path}")


def _verify(directory: Path, bundle: NativeBundle) -> None:
    _owned(directory, directory=True, private=True)
    _owned(directory / "bin", directory=True, private=True)
    manifest_path = directory / "selection-manifest.json"
    if not _regular(manifest_path) or manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("Native Codex capability manifest is missing or invalid")
    _owned(manifest_path)
    if _sha256(manifest_path) != bundle.manifest_sha256:
        raise ValueError("Native Codex capability manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or set(files) != _FILES - {"selection-manifest.json"}:
        raise ValueError("Native Codex bundle file manifest is incomplete")
    for name, digest in files.items():
        path = directory / name
        if not _regular(path) or _sha256(path) != digest:
            raise ValueError(f"Native Codex bundled file checksum mismatch: {name}")
        _owned(path)
    if (manifest.get("binary_sha256") != files["bin/codex"]
            or type(manifest.get("protocol")) is not int or manifest["protocol"] != 1
            or manifest.get("feature") != "bello_native_selection"
            or manifest.get("transport_timeout_seconds") != 315):
        raise ValueError("Native Codex bundle does not implement the required selection protocol")
    for name in ("bin/codex", "bin/codex-code-mode-host"):
        if not os.access(directory / name, os.X_OK):
            raise ValueError(f"Native Codex bundled executable is not executable: {name}")


def _download(bundle: NativeBundle, destination: Path) -> None:
    if not bundle.url.startswith("https://github.com/Makson179/Bello/releases/download/"):
        raise ValueError("Native Codex download must use the pinned Bello release")
    request = Request(bundle.url, headers={"User-Agent": "Bello-native-codex-installer"})
    digest = hashlib.sha256()
    size = 0
    with urlopen(request, timeout=60) as response, destination.open("xb") as output:
        if not response.geturl().startswith("https://"):
            raise ValueError("Native Codex download was redirected to an insecure URL")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > _MAX_DOWNLOAD:
                raise ValueError("Native Codex download exceeds its size limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != bundle.archive_sha256:
        raise ValueError("Native Codex archive checksum mismatch")


def _unpack(archive: Path, destination: Path) -> None:
    """Extract only the small, fixed regular-file layout; never tar links/modes."""
    seen = set()
    size = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            name = member.name.removeprefix("./")
            if member.isdir() and name.rstrip("/") in {".", "bin"}:
                continue
            if (name not in _FILES or name in seen or not member.isfile()
                    or PurePosixPath(name).is_absolute() or member.size < 0):
                raise ValueError(f"Unexpected entry in native Codex archive: {member.name}")
            seen.add(name)
            size += member.size
            if size > _MAX_UNPACKED:
                raise ValueError("Native Codex archive expands beyond its size limit")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable native Codex archive entry: {name}")
            with stream, target.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(0o700 if name.startswith("bin/") else 0o600)
    if seen != _FILES:
        raise ValueError("Native Codex archive is incomplete")


def ensure_native_selection() -> tuple[list[str], Path | None]:
    """Return native app-server command + manifest, without changing user config."""
    override = os.environ.get("BELLO_CODEX_BINARY")
    explicit_manifest = os.environ.get("BELLO_CODEX_SELECTION_MANIFEST")
    if override or explicit_manifest:
        return ([override or "codex", "app-server", "--listen", "stdio://"],
                Path(explicit_manifest).expanduser().absolute() if explicit_manifest else None)
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    key = (platform.system(), machine)
    bundle = BUNDLES.get(key)
    if bundle is None:
        raise RuntimeError(
            f"No verified native Codex log-distiller download is available for {key[0]}/{key[1]}. "
            "Set BELLO_CODEX_BINARY and BELLO_CODEX_SELECTION_MANIFEST to a compatible build; "
            "see docs/native-codex-selection.md. Other engines and distiller-off runs do not need it."
        )
    base = Path(os.environ.get("BELLO_RUNTIME_DIR", str(Path.home() / ".bello" / "runtime"))).expanduser().absolute()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    _owned(base, directory=True)
    root = base / "native-codex"
    if root.is_symlink():
        raise ValueError("Native Codex cache root must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _owned(root, directory=True, private=True)
    destination = root / bundle.archive_sha256
    lock_path = root / f".{bundle.archive_sha256}.lock"
    if lock_path.is_symlink():
        raise ValueError("Native Codex installation lock must not be a symbolic link")
    if lock_path.exists():
        _owned(lock_path)
    with FileLock(lock_path):
        if destination.exists() or destination.is_symlink():
            # Never repair/replace a changed or active executable underneath a run.
            _verify(destination, bundle)
        else:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temporary:
                staging = Path(temporary)
                archive = staging / "bundle.tar.gz"
                _download(bundle, archive)
                unpacked = staging / "unpacked"
                unpacked.mkdir(mode=0o700)
                _unpack(archive, unpacked)
                _verify(unpacked, bundle)
                unpacked.rename(destination)
    return ([str(destination / "bin" / "codex"), "app-server", "--listen", "stdio://"],
            destination / "selection-manifest.json")
