from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from supervisor import __version__
from supervisor.executables import resolve_trusted_executable


DISTRIBUTION_NAME = "bello"
SKIP_UPDATE_CHECK_ENV = "BELLO_SKIP_UPDATE_CHECK"
REMOTE_CHECK_TIMEOUT_SECONDS = 8.0
UPDATE_COMMAND_TIMEOUT_SECONDS = 300.0
NONINTERACTIVE_UPDATE_EXIT_CODE = 17
PYPI_JSON_BASE_URL = "https://pypi.org/pypi"


class UpdateCheckError(RuntimeError):
    pass


class UpdateState(str, Enum):
    CURRENT = "current"
    OUTDATED = "outdated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstallInfo:
    package_name: str
    version: str
    install_mode: str
    metadata_available: bool = True
    metadata_location: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class UpdateStatus:
    state: UpdateState
    install_info: InstallInfo
    latest_version: str | None = None
    warning: str | None = None

    @property
    def is_current(self) -> bool:
        return self.state == UpdateState.CURRENT

    @property
    def is_outdated(self) -> bool:
        return self.state == UpdateState.OUTDATED


@dataclass(frozen=True)
class PreparedRuntime:
    version: str
    directory: str


def skip_update_check_enabled(environ: dict[str, str] | None = None) -> bool:
    value = (environ or os.environ).get(SKIP_UPDATE_CHECK_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_install_info(distribution_name: str = DISTRIBUTION_NAME) -> InstallInfo:
    version = __version__
    metadata_location: str | None = None
    warning: str | None = None
    metadata_available = True

    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        metadata_available = False
        warning = f"package metadata for {distribution_name!r} was not found"
    else:
        version = getattr(dist, "version", None) or version
        metadata_location = _distribution_location(dist)

    return InstallInfo(
        package_name=distribution_name,
        version=version,
        install_mode=detect_install_mode(),
        metadata_available=metadata_available,
        metadata_location=metadata_location,
        warning=warning,
    )


def check_for_update(install_info: InstallInfo | None = None) -> UpdateStatus:
    info = install_info or read_install_info()
    try:
        latest = latest_pypi_version(info.package_name)
        installed_version = Version(info.version)
        latest_version = Version(latest)
    except (UpdateCheckError, InvalidVersion) as exc:
        return UpdateStatus(UpdateState.UNKNOWN, info, warning=str(exc))

    if latest_version > installed_version:
        return UpdateStatus(UpdateState.OUTDATED, info, latest_version=latest)
    return UpdateStatus(UpdateState.CURRENT, info, latest_version=latest)


def latest_pypi_version(
    package_name: str,
    *,
    timeout: float = REMOTE_CHECK_TIMEOUT_SECONDS,
    base_url: str = PYPI_JSON_BASE_URL,
) -> str:
    quoted_name = urllib.parse.quote(package_name, safe="")
    url = f"{base_url.rstrip('/')}/{quoted_name}/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateCheckError(f"package {package_name!r} was not found on PyPI") from exc
        raise UpdateCheckError(f"PyPI returned HTTP {exc.code} while checking for updates") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateCheckError(f"could not reach PyPI while checking for updates: {reason}") from exc
    except TimeoutError as exc:
        raise UpdateCheckError("PyPI update check timed out") from exc

    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("PyPI returned invalid JSON while checking for updates") from exc

    if not isinstance(payload, dict):
        raise UpdateCheckError("PyPI returned an invalid response while checking for updates")
    info = payload.get("info")
    if not isinstance(info, dict):
        raise UpdateCheckError("PyPI response did not include package info")
    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        raise UpdateCheckError("PyPI response did not include a latest version")
    return version


def run_update(info: InstallInfo) -> PreparedRuntime:
    # Remember optional support before pip replaces the package metadata.
    with_claude = _claude_is_installed()
    command = update_command(info)
    env = _pipx_target()[1] if info.install_mode == "pipx" else None
    _run_package_command(command, **({"env": env} if env is not None else {}))
    try:
        prepared = prepare_runtime(with_claude=with_claude)
    except UpdateCheckError as exc:
        raise UpdateCheckError(
            "The Bello package update finished, but execution dependencies are not ready. "
            f"Run `bello update` again to finish setup.\n\n{exc}"
        ) from exc
    if Version(prepared.version) <= Version(info.version):
        raise UpdateCheckError(
            f"Execution dependencies are ready, but the package manager left Bello at {prepared.version}. "
            "Check this installation's package source or version pin before retrying the update."
        )
    return prepared


def _run_package_command(
    command: list[str], *, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=UPDATE_COMMAND_TIMEOUT_SECONDS,
            check=False,
            **({"env": env} if env is not None else {}),
        )
    except OSError as exc:
        raise UpdateCheckError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateCheckError("Bello update command timed out") from exc

    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip()
        detail = f"\n\n{output}" if output else ""
        raise UpdateCheckError(f"Bello update command failed.{detail}")
    return completed


def prepare_runtime(*, with_claude: bool | None = None) -> PreparedRuntime:
    """Use the newly installed package, never the updater's cached modules.

    Isolated mode excludes the caller's project and PYTHONPATH from imports.
    The same interpreter keeps pipx/venv selection independent of PATH.
    """
    if with_claude is None:
        with_claude = _claude_is_installed()
    command = [sys.executable, "-I", "-m", "supervisor.update_check", "--prepare-runtime"]
    if with_claude:
        command.append("--with-claude")
    try:
        # npm has its own network timeouts. Do not kill its parent mid-install
        # and leave an orphan writing to a runtime after its lock is released.
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise UpdateCheckError(f"Could not prepare Bello's execution dependencies: {exc}") from exc
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip()
        raise UpdateCheckError(f"Could not prepare Bello's execution dependencies.\n{output}")
    try:
        # Dependency installers may print progress before the final receipt.
        receipt = json.loads(completed.stdout.strip().splitlines()[-1])
        version, directory = receipt["version"], receipt["runtime_directory"]
        if not isinstance(version, str) or not isinstance(directory, str) or not directory:
            raise ValueError("invalid preparation receipt")
        Version(version)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise UpdateCheckError("Execution dependency preparation did not return a valid result") from exc
    return PreparedRuntime(version=version, directory=directory)


def _claude_is_installed() -> bool:
    try:
        metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return False
    return True


def _dependency_install_command(requirement: str) -> tuple[list[str], dict[str, str] | None]:
    """Address only this interpreter's environment, including pipx's uv backend."""
    if not _running_inside_venv():
        raise UpdateCheckError("Cannot update Claude support outside a pipx or virtual environment")
    if detect_install_mode() != "pipx":
        return [sys.executable, "-I", "-m", "pip", "install", requirement], None

    name, env = _pipx_target()
    # runpip respects the environment's recorded pip/uv backend. A uv-backed
    # pipx environment need not contain an importable pip module.
    return [_pipx_executable(), "runpip", name, "install", requirement], env


def _pipx_target() -> tuple[str, dict[str, str]]:
    prefix = Path(sys.prefix).resolve()
    # pipx derives the target as PIPX_HOME/venvs/<canonical name>. Do not let an
    # ambient PIPX_HOME or the unsuffixed distribution name select another app.
    target = prefix.parent / canonicalize_name(prefix.name)
    if (
        prefix.parent.name != "venvs"
        or not (prefix / "pipx_metadata.json").is_file()
        or target.resolve() != prefix
    ):
        raise UpdateCheckError("Cannot identify this Bello installation's pipx environment safely")
    env = dict(os.environ)
    env["PIPX_HOME"] = str(prefix.parent.parent)
    return prefix.name, env


def _pipx_executable() -> str:
    pipx = resolve_trusted_executable("pipx", cwd=Path.cwd(), windows=sys.platform == "win32")
    if not pipx:
        raise UpdateCheckError("pipx is required to update this Bello installation")
    return pipx


def _ensure_claude_dependency() -> None:
    """Honor this installed release's Claude pin, not the latest SDK release."""
    dist = metadata.distribution(DISTRIBUTION_NAME)
    candidates = [Requirement(raw) for raw in dist.requires or []]
    requirements = [
        req for req in candidates
        if canonicalize_name(req.name) == "claude-agent-sdk"
        and (req.marker is None or req.marker.evaluate({"extra": "claude"}))
    ]
    if len(requirements) != 1 or not requirements[0].specifier or requirements[0].url:
        raise UpdateCheckError("Installed Bello metadata does not specify its Claude SDK dependency")
    requirement = requirements[0]
    try:
        version = metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        version = None
    if version is None or not requirement.specifier.contains(version, prereleases=True):
        command, env = _dependency_install_command(f"{requirement.name}{requirement.specifier}")
        _run_package_command(command, **({"env": env} if env is not None else {}))
        if not requirement.specifier.contains(metadata.version("claude-agent-sdk"), prereleases=True):
            raise UpdateCheckError("Claude SDK installation does not match this Bello release")
    # File-level readiness only. Never log in or make a provider request here.
    from supervisor.runtime.claude import ClaudeBackend
    ClaudeBackend._bundled_cli_path()


def _prepare_installed_runtime(*, with_claude: bool) -> PreparedRuntime:
    from supervisor.runtime.install import ensure_worker
    if with_claude or _claude_is_installed():
        _ensure_claude_dependency()
    directory = ensure_worker()
    return PreparedRuntime(version=metadata.version(DISTRIBUTION_NAME), directory=str(directory))


def update_command(info: InstallInfo) -> list[str]:
    if info.install_mode == "pipx":
        name, _ = _pipx_target()
        return [_pipx_executable(), "upgrade", name]
    if _running_inside_venv():
        return [sys.executable, "-I", "-m", "pip", "install", "--upgrade", info.package_name]
    raise UpdateCheckError(manual_update_message(info))


def manual_update_message(info: InstallInfo) -> str:
    return "\n".join(
        [
            "Could not update Bello automatically.",
            "",
            "Try:",
            f"  pipx upgrade {info.package_name}",
            "",
            "or:",
            f"  pipx install --force {info.package_name}",
        ]
    )


def detect_install_mode() -> str:
    prefix = Path(sys.prefix)
    if (prefix / "pipx_metadata.json").exists():
        return "pipx"
    prefix_parts = {part.lower() for part in prefix.parts}
    if "pipx" in prefix_parts and "venvs" in prefix_parts:
        return "pipx"
    if _running_inside_venv():
        return "venv"
    return "system"


def _running_inside_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _distribution_location(dist: metadata.Distribution) -> str | None:
    try:
        return str(dist.locate_file(""))
    except Exception:
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prepare this installed Bello release's execution dependencies")
    parser.add_argument("--prepare-runtime", action="store_true", required=True)
    parser.add_argument("--with-claude", action="store_true")
    arguments = parser.parse_args()
    try:
        prepared = _prepare_installed_runtime(with_claude=arguments.with_claude)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"version": prepared.version, "runtime_directory": prepared.directory}))
