"""The official Claude Code CLI used by Bello's ``claude-code`` subscription backend.

Bello accepts exactly two sources for this executable and nothing else: no PATH
lookup, checkout file, caller-supplied path, environment override, other CLI
version, or API route.

1. An explicitly pinned managed CLI/SDK pair on supported platforms. The
   managed CLI takes precedence over the SDK's bundled CLI, but the SDK's exact
   version and bundled-CLI metadata declaration must both match the pair.
2. Otherwise, the CLI bundled in the installed ``claude-agent-sdk`` wheel, or
   a same-build managed fallback where a release has no bundled executable.

Managed builds are downloaded only from Anthropic's release host, checked
against the exact size and SHA-256 pinned below, and stored in Bello's private
runtime cache. A missing or mismatched explicit pair never falls back to an
SDK bundle or a different executable.

Preparation (download) happens only in explicit setup commands, ``bello
update`` and before a run whose roles use ``claude-code``; ordinary readiness
checks only verify local files. Neither path logs in or sends a model request.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata as metadata
import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, Literal
import urllib.error
from urllib.request import Request, urlopen

from supervisor.appserver import AppServerError
from supervisor.filesystem_safety import is_link_or_reparse
from supervisor.state import FileLock


SDK_DISTRIBUTION = "claude-agent-sdk"
OFFICIAL_RELEASE_BASE = "https://downloads.claude.ai/claude-code-releases/"
INSTALL_COMMAND = "bello runtime install claude-code"

FailureKind = Literal["missing-sdk", "missing-bundle", "not-prepared", "sdk-mismatch", "invalid-cache", "download"]


class ClaudeCliError(AppServerError):
    """Readiness failure with a user-facing message free of credentials or payloads."""

    def __init__(self, message: str, *, kind: FailureKind):
        super().__init__(message)
        self.kind = kind


class BundledCliMissing(ClaudeCliError):
    """The SDK imports, but its wheel did not contain the bundled CLI."""

    def __init__(self, message: str = "the official CLI bundled with claude-agent-sdk is missing"):
        super().__init__(message, kind="missing-bundle")


@dataclass(frozen=True)
class OfficialCliRelease:
    """One exact official Claude Code build, identified by Anthropic's release manifest."""

    cli_version: str
    sdk_version: str
    platform: str
    binary: str
    size: int
    sha256: str
    # When present, this is the exact metadata declaration accepted from the
    # pinned SDK, not the version of the managed executable selected at runtime.
    # It also opts into managed-first resolution; None keeps legacy bundle-first
    # resolution and same-build fallback for releases without a bundled CLI.
    sdk_bundled_cli_version: str | None = None

    @property
    def url(self) -> str:
        return f"{OFFICIAL_RELEASE_BASE}{self.cli_version}/{self.platform}/{self.binary}"


# The explicit SDK 0.2.164 / CLI 2.1.293 pair accepts only the SDK's 2.1.292
# bundled-CLI metadata, while always executing the managed 2.1.293 build.
# Sizes and SHA-256 values come from Anthropic's 2.1.293 release manifest
# (<version>/manifest.json on the host above). Its detached PGP signature is
# not checked here: these source-pinned hashes are the trust anchor. Windows
# CI additionally checks the downloaded executable's Authenticode signature.
# The Linux artifacts below target glibc, not musl/Alpine. Windows on ARM is
# not a supported Bello platform.
MANAGED_RELEASES: dict[tuple[str, str], OfficialCliRelease] = {
    ("Darwin", "arm64"): OfficialCliRelease(
        cli_version="2.1.293", sdk_version="0.2.164", sdk_bundled_cli_version="2.1.292",
        platform="darwin-arm64", binary="claude", size=236_330_608,
        sha256="4e21122a227857da1178aca3299700c1fd7f2b77c93f12e73c2c76db796a105e",
    ),
    ("Darwin", "x86_64"): OfficialCliRelease(
        cli_version="2.1.293", sdk_version="0.2.164", sdk_bundled_cli_version="2.1.292",
        platform="darwin-x64", binary="claude", size=244_819_072,
        sha256="267af22d4eb187b8d65d1592e6fabf57b1df6c254913d5a6c5d8b956a02cd002",
    ),
    ("Linux", "arm64"): OfficialCliRelease(
        cli_version="2.1.293", sdk_version="0.2.164", sdk_bundled_cli_version="2.1.292",
        platform="linux-arm64", binary="claude", size=252_108_792,
        sha256="a43629e888f0a7d96c5e8de62abf44852433a7ff2481574688db3e5b6399491f",
    ),
    ("Linux", "x86_64"): OfficialCliRelease(
        cli_version="2.1.293", sdk_version="0.2.164", sdk_bundled_cli_version="2.1.292",
        platform="linux-x64", binary="claude", size=252_755_128,
        sha256="8968405e26db478af44eabc4635ab5ca557057b702a54460a59c13e1b253e978",
    ),
    ("Windows", "x86_64"): OfficialCliRelease(
        cli_version="2.1.293", sdk_version="0.2.164", sdk_bundled_cli_version="2.1.292",
        platform="win32-x64", binary="claude.exe", size=256_155_808,
        sha256="8693c4a02dde7441d0066ede68af8ddfc408bb982d77e12b506286268224e6fa",
    ),
}
_MAX_REDIRECTED_URL = 4096


@dataclass(frozen=True)
class OfficialCli:
    path: Path
    source: Literal["sdk-bundle", "managed-download"]
    cli_version: str | None = None

    def describe(self) -> str:
        if self.source == "sdk-bundle":
            return "official CLI bundled with claude-agent-sdk"
        return f"official Claude Code CLI {self.cli_version} (Bello-verified download)"


def sdk_bundled_cli() -> Path:
    """Return the CLI inside the installed SDK wheel, without executing it."""
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise ClaudeCliError(
            "Claude Code support requires the pinned claude-agent-sdk package", kind="missing-sdk"
        ) from exc
    name = "claude.exe" if os.name == "nt" else "claude"
    path = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / name
    if not path.is_file():
        raise BundledCliMissing()
    return path


def managed_release() -> OfficialCliRelease | None:
    from supervisor.runtime.native_codex_install import _platform_key

    return MANAGED_RELEASES.get(_platform_key())


def managed_cli_path(release: OfficialCliRelease) -> Path:
    return _runtime_base() / "claude-code" / release.sha256 / release.binary


def resolve_official_cli(
    *,
    prepare: bool = False,
    bundled: Callable[[], Path] = sdk_bundled_cli,
    download: Callable[[OfficialCliRelease, Path], None] | None = None,
) -> OfficialCli:
    """Return the single trusted CLI, optionally preparing the managed download.

    ``prepare=False`` is the file-level readiness contract shared by backend
    startup and doctor. ``prepare=True`` may download the pinned build first;
    installation, ``bello update``, login and run preflight use it.
    """
    release = managed_release()
    if release is None or release.sdk_bundled_cli_version is None:
        try:
            return OfficialCli(Path(bundled()), "sdk-bundle", sdk_declared_cli_version())
        except BundledCliMissing:
            if release is None:
                raise
    check_sdk_pairing(release)
    path = _install(release, download or _download) if prepare else _verified(release)
    return OfficialCli(path, "managed-download", release.cli_version)


def sdk_declared_cli_version() -> str | None:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except Exception:
        return None
    return __cli_version__ if isinstance(__cli_version__, str) else None


def check_sdk_pairing(release: OfficialCliRelease) -> None:
    try:
        installed = metadata.version(SDK_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise ClaudeCliError(
            "Claude Code support requires the pinned claude-agent-sdk package", kind="missing-sdk"
        ) from exc
    declared = sdk_declared_cli_version()
    expected_declaration = (release.cli_version if release.sdk_bundled_cli_version is None
                            else release.sdk_bundled_cli_version)
    accepted_declarations = ((release.sdk_bundled_cli_version,)
                             if release.sdk_bundled_cli_version is not None
                             else (None, release.cli_version))
    if installed != release.sdk_version or declared not in accepted_declarations:
        raise ClaudeCliError(
            f"installed claude-agent-sdk {installed} (bundled-CLI version {declared or 'unknown'}) is not the "
            f"release Bello pairs with its verified Claude Code CLI {release.cli_version} "
            f"(claude-agent-sdk {release.sdk_version}, bundled-CLI declaration {expected_declaration}). "
            "Run `bello update` to restore this Bello "
            "release's pinned SDK; Bello will not use a different SDK/CLI pairing",
            kind="sdk-mismatch",
        )


def _runtime_base() -> Path:
    configured = os.environ.get("BELLO_RUNTIME_DIR", str(Path.home() / ".bello" / "runtime"))
    return Path(configured).expanduser().absolute()


def _cache():
    # The native Codex installer owns Bello's private-cache and Windows ACL
    # checks; reuse them rather than maintaining a second implementation.
    from supervisor.runtime import native_codex_install

    return native_codex_install


def _not_prepared(release: OfficialCliRelease) -> ClaudeCliError:
    return ClaudeCliError(
        f"the official Claude Code CLI {release.cli_version} for this platform is not prepared yet. "
        f"Run `{INSTALL_COMMAND}` to download it from downloads.claude.ai and verify Bello's pinned "
        "SHA-256 (no login or model request)",
        kind="not-prepared",
    )


def _verified(release: OfficialCliRelease) -> Path:
    base = _runtime_base()
    directory = base / "claude-code" / release.sha256
    try:
        present = directory.exists() or is_link_or_reparse(directory)
    except OSError as exc:
        raise _invalid_cache(directory, exc) from exc
    if not present:
        raise _not_prepared(release)
    try:
        cache = _cache()
        cache._reject_windows_reparse_ancestors(directory)
        cache._owned(base, directory=True)
        cache._owned(directory.parent, directory=True, private=True)
        _verify_directory(directory, release)
    except (OSError, ValueError) as exc:
        raise _invalid_cache(directory, exc) from exc
    return directory / release.binary


def _invalid_cache(directory: Path, error: BaseException) -> ClaudeCliError:
    return ClaudeCliError(
        f"Bello's cached Claude Code CLI failed verification ({error}). It is never repaired or replaced "
        f"automatically, because a run may be using it; remove {directory} and run `{INSTALL_COMMAND}`",
        kind="invalid-cache",
    )


def _unusable_runtime_directory(base: Path, error: BaseException) -> ClaudeCliError:
    # Before any cache exists: the location itself (or its containing
    # directory) failed the shared private-cache checks. Name it; change nothing.
    return ClaudeCliError(
        f"Bello's private runtime directory {base} failed verification ({error}). The Claude Code CLI was "
        "not installed and existing permissions were not changed; use the per-user default ~/.bello/runtime "
        f"or a BELLO_RUNTIME_DIR that other accounts cannot modify, then run `{INSTALL_COMMAND}`",
        kind="invalid-cache",
    )


def _verify_directory(directory: Path, release: OfficialCliRelease) -> None:
    cache = _cache()
    cache._owned(directory, directory=True, private=True)
    if {child.name for child in directory.iterdir()} != {release.binary}:
        raise ValueError("the Claude Code CLI cache contains unexpected files")
    path = directory / release.binary
    cache._owned(path)
    metadata_ = path.lstat()
    if not stat.S_ISREG(metadata_.st_mode) or metadata_.st_size != release.size:
        raise ValueError("the cached Claude Code CLI does not have the pinned size")
    # Always hash the bytes. Size, mtime, inode and device can all stay the
    # same after an in-place rewrite (coarse NTFS timestamps, restored mtime),
    # so no metadata-keyed memo may stand in for the pinned SHA-256.
    if cache._sha256(path) != release.sha256:
        raise ValueError("the cached Claude Code CLI checksum does not match the pinned official build")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise ValueError("the cached Claude Code CLI is not executable")


def _install(release: OfficialCliRelease, download: Callable[[OfficialCliRelease, Path], None]) -> Path:
    cache = _cache()
    base = _runtime_base()
    root = base / "claude-code"
    destination = root / release.sha256
    try:
        cache._reject_windows_reparse_ancestors(base)
        if cache._IS_WINDOWS:
            cache._private_directory(base, parents=True)
        else:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache._owned(base, directory=True)
        cache._private_directory(root)
        lock_path = root / f".{release.sha256}.lock"
        if is_link_or_reparse(lock_path):
            raise ValueError("the Claude Code CLI installation lock must not be a link or reparse point")
        if lock_path.exists():
            cache._owned(lock_path)
    except (OSError, ValueError) as exc:
        raise _unusable_runtime_directory(base, exc) from exc
    with FileLock(lock_path):
        try:
            cache._owned(lock_path)
            present = destination.exists() or is_link_or_reparse(destination)
        except (OSError, ValueError) as exc:
            raise _invalid_cache(destination, exc) from exc
        if present:
            # Never repair or replace an executable a running Bello may use.
            try:
                _verify_directory(destination, release)
            except (OSError, ValueError) as exc:
                raise _invalid_cache(destination, exc) from exc
            return destination / release.binary
        try:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temporary:
                staging = Path(temporary)
                cache._owned(staging, directory=True, private=True)
                prepared = staging / "cli"
                prepared.mkdir(mode=0o700)
                target = prepared / release.binary
                download(release, target)
                target.chmod(0o700)
                _verify_directory(prepared, release)
                prepared.rename(destination)
            _verify_directory(destination, release)
        except ClaudeCliError:
            raise
        except (OSError, ValueError) as exc:
            raise ClaudeCliError(
                f"could not install the official Claude Code CLI {release.cli_version}: {exc}. "
                "Nothing was installed; no other executable is used instead",
                kind="download",
            ) from exc
    return destination / release.binary


def _download(release: OfficialCliRelease, destination: Path) -> None:
    """Stream the pinned build; any size or digest difference aborts it."""
    if not release.url.startswith(OFFICIAL_RELEASE_BASE):
        raise ValueError("the Claude Code CLI download must use Anthropic's official release host")
    request = Request(release.url, headers={"User-Agent": "Bello-claude-code-installer"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(request, timeout=60) as response, destination.open("xb") as output:
            final = response.geturl()
            if not isinstance(final, str) or len(final) > _MAX_REDIRECTED_URL or not final.startswith("https://"):
                raise ValueError("the Claude Code CLI download was redirected to an insecure URL")
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.strip().isdigit() and int(declared) != release.size:
                raise ValueError("the Claude Code CLI download does not have the pinned size")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > release.size:
                    raise ValueError("the Claude Code CLI download exceeds its pinned size")
                digest.update(chunk)
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise ClaudeCliError(
            f"could not download the official Claude Code CLI {release.cli_version} from downloads.claude.ai "
            f"(HTTP {exc.code}). Nothing was installed; no other executable is used instead",
            kind="download",
        ) from exc
    except urllib.error.URLError as exc:
        raise ClaudeCliError(
            f"could not reach downloads.claude.ai for the official Claude Code CLI {release.cli_version} "
            f"({exc.reason}). The download service can be unavailable in some regions. Nothing was "
            "installed; no other executable is used instead",
            kind="download",
        ) from exc
    except TimeoutError as exc:
        raise ClaudeCliError(
            f"downloading the official Claude Code CLI {release.cli_version} timed out; nothing was installed",
            kind="download",
        ) from exc
    if size != release.size or digest.hexdigest() != release.sha256:
        raise ValueError("the Claude Code CLI download checksum does not match the pinned official build")
