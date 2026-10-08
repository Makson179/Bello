#!/usr/bin/env python3
"""Verify Bello's official Claude Code CLI on a real native Windows runner.

Phases (run in order on a fresh runner with BELLO_RUNTIME_DIR unset, so the
proof covers the default per-user runtime directory %USERPROFILE%\\.bello\\runtime):

* ``sdk``     - claude-agent-sdk is the pinned release 0.2.164, importable and
                declaring bundled CLI 2.1.292 (wheel or sdist installation).
* ``before``  - readiness fails closed with the setup command; doctor warns.
                Nothing is downloaded.
* ``runner-temp`` - informational and read-only: whether Bello's containing-
                directory policy accepts RUNNER_TEMP, with the sanitized reason
                if not. Nothing is created or downloaded; it never fails the job.
* ``after``   - after ``bello runtime install claude-code``: the verified cache
                in the default directory is used, its SHA-256 matches the pin,
                real Windows ACLs are private from ``.bello`` down to claude.exe,
                Windows reports a valid Authenticode signature by Anthropic, and
                ``claude.exe --version`` reports 2.1.293, not the older SDK bundle.
* ``metadata``- a signed-out metadata handshake through Bello's own backend
                (SDK initialize/get_server_info in an isolated CLAUDE_CONFIG_DIR)
                lists claude-sonnet-5-5 and claude-haiku-5-5. No login, no prompt,
                and no model request; any credential variable aborts the check.

Exit status is non-zero on the first failed check. Output contains no
environment values and no provider payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"OK: {message}")


def check_sdk() -> None:
    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    from supervisor.runtime.claude_cli import check_sdk_pairing, managed_release

    release = managed_release()
    if release is None:
        fail("this runner is not a platform with a managed Claude Code CLI pin")
    version = metadata.version("claude-agent-sdk")
    expected_bundle = release.sdk_bundled_cli_version or release.cli_version
    if version != release.sdk_version or __cli_version__ != expected_bundle:
        fail(f"claude-agent-sdk {version} declares CLI {__cli_version__}; expected {release.sdk_version}/"
             f"{expected_bundle}")
    check_sdk_pairing(release)
    bundled = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / "claude.exe"
    ok(f"claude-agent-sdk {version} declares CLI {__cli_version__}; bundled executable present: "
       f"{bundled.is_file()}; explicitly paired managed CLI: {release.cli_version}")


def default_runtime() -> Path:
    """Bello's own default per-user runtime directory, as the product resolves it."""
    from supervisor.runtime import claude_cli

    runtime = claude_cli._runtime_base()
    if runtime != (Path.home() / ".bello" / "runtime").absolute():
        fail("Bello did not resolve its default per-user runtime directory")
    return runtime


def check_before() -> None:
    from supervisor import doctor
    from supervisor.appserver import AppServerError
    from supervisor.runtime import claude_cli
    from supervisor.runtime.claude import ClaudeBackend

    def forbidden(*_args, **_kwargs):
        fail("readiness verification attempted a download")

    runtime = default_runtime()
    claude_cli._download = forbidden
    try:
        ClaudeBackend._official_cli()
    except claude_cli.ClaudeCliError as exc:
        if exc.kind != "not-prepared" or "bello runtime install claude-code" not in str(exc):
            fail(f"unexpected readiness failure kind {exc.kind}")
    else:
        fail("an unprepared runner reported a ready Claude Code CLI")
    result = doctor._claude_dependency_result(ClaudeBackend, AppServerError)
    if result.level != "warn" or "bello runtime install claude-code" not in (result.detail or ""):
        fail("doctor did not report the missing preparation")
    ok(f"unprepared state fails closed with the setup command; nothing was downloaded ({runtime})")


def check_runner_temp() -> None:
    """Record why RUNNER_TEMP cannot contain Bello's private cache (read-only)."""
    from supervisor.runtime import native_codex_install

    temp = os.environ.get("RUNNER_TEMP")
    if not temp:
        print("INFO: RUNNER_TEMP is not set; nothing to inspect")
        return
    try:
        native_codex_install._windows_private_acl(Path(temp), parent=True)
    except (OSError, ValueError) as exc:
        print(f"INFO: Bello's private-cache policy refuses RUNNER_TEMP as a containing directory: {exc}")
    else:
        print("INFO: Bello's private-cache policy accepts RUNNER_TEMP as a containing directory")


def _authenticode(path: Path) -> dict:
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:BELLO_VERIFY_PATH; "
        "[pscustomobject]@{Status=[string]$s.Status; Subject=[string]$s.SignerCertificate.Subject} "
        "| ConvertTo-Json -Compress"
    )
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        fail("PowerShell is required to read the Authenticode signature")
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=120, env={**os.environ, "BELLO_VERIFY_PATH": str(path)},
    )
    if completed.returncode != 0:
        fail("Get-AuthenticodeSignature failed")
    return json.loads(completed.stdout)


def check_after() -> None:
    from supervisor import doctor
    from supervisor.appserver import AppServerError
    from supervisor.runtime import claude_cli, native_codex_install
    from supervisor.runtime.claude import ClaudeBackend

    cli = ClaudeBackend._official_cli()
    release = claude_cli.managed_release()
    runtime = default_runtime()
    if cli.source != "managed-download" or cli.path != runtime / "claude-code" / release.sha256 / release.binary:
        fail("Bello did not use its verified private cache in the default per-user runtime directory")
    digest = hashlib.sha256(cli.path.read_bytes()).hexdigest()
    if digest != release.sha256 or cli.path.stat().st_size != release.size:
        fail("the cached CLI does not match the pinned official build")
    ok(f"verified {cli.path} (SHA-256 {digest})")
    # Real OS ACLs for everything Bello created: owner and grants limited to
    # this user, SYSTEM and Administrators (the same validator the cache uses).
    try:
        for path in (runtime.parent, runtime, runtime / "claude-code", cli.path.parent, cli.path):
            native_codex_install._windows_private_acl(path)
    except (OSError, ValueError) as exc:
        fail(f"private Windows ACL check failed: {exc}")
    ok(f"private Windows ACLs from {runtime.parent} down to {release.binary}")
    signature = _authenticode(cli.path)
    if signature.get("Status") != "Valid" or "Anthropic" not in (signature.get("Subject") or ""):
        fail(f"Authenticode status {signature.get('Status')!r}, signer {signature.get('Subject')!r}")
    ok(f"Authenticode signature valid: {signature['Subject']}")
    with tempfile.TemporaryDirectory() as config_dir:
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("ANTHROPIC_", "CLAUDE_"))}
        environment["CLAUDE_CONFIG_DIR"] = config_dir
        completed = subprocess.run([str(cli.path), "--version"], capture_output=True, text=True, timeout=120,
                                   env=environment, stdin=subprocess.DEVNULL)
    if completed.returncode != 0 or release.cli_version not in completed.stdout:
        fail(f"claude.exe --version did not report {release.cli_version}")
    ok(f"claude.exe --version: {completed.stdout.strip()}")
    result = doctor._claude_dependency_result(ClaudeBackend, AppServerError)
    if result.level != "ok" or release.cli_version not in result.message:
        fail("doctor did not report the prepared CLI as ready")
    ok(result.message)


def check_metadata() -> None:
    from supervisor.runtime.claude import ClaudeBackend, _DIRECT_CREDENTIAL_ENV, _PROVIDER_SWITCH_ENV

    present = sorted(name for name in (*_DIRECT_CREDENTIAL_ENV, *_PROVIDER_SWITCH_ENV) if os.environ.get(name))
    if present:
        fail("credential or provider variables are set; refusing the metadata check: " + ", ".join(present))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir or (Path(config_dir).exists() and any(Path(config_dir).iterdir())):
        fail("set CLAUDE_CONFIG_DIR to a new empty directory so no existing login can be used")

    async def emit(_event):
        return None

    async def tool(_request):
        return {"content": [], "isError": True}

    async def read() -> list[dict]:
        with tempfile.TemporaryDirectory() as state:
            backend = ClaudeBackend(Path(state), emit, tool_handler=tool)
            # The production metadata path: no auth probe, no thread, no prompt.
            return await asyncio.wait_for(backend._read_model_catalog(), 180)

    entries = asyncio.run(read())
    by_id = {entry["id"]: entry for entry in entries}
    for family in ("sonnet", "haiku"):
        identifier = f"claude-{family}-5-5"
        model = by_id.get(identifier)
        if model is None:
            fail(f"the pinned Windows CLI did not advertise {identifier}: " + ", ".join(sorted(by_id)))
        efforts = model["supportedEfforts"]
        if efforts != ["low", "medium", "high", "xhigh", "max"]:
            fail(f"unexpected {identifier} efforts {efforts}")
        alias = by_id.get(family, {})
        if alias.get("resolvedModel") != identifier:
            fail(f"the {family} alias did not resolve to {identifier}")
        ok(f"signed-out metadata lists {identifier} with efforts " + ", ".join(efforts)
           + f"; alias {family} -> {identifier}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    phases = {"sdk": check_sdk, "before": check_before, "runner-temp": check_runner_temp,
              "after": check_after, "metadata": check_metadata}
    parser.add_argument("phase", choices=list(phases))
    arguments = parser.parse_args()
    if sys.platform != "win32":
        fail("run this verifier on native Windows")
    if "BELLO_RUNTIME_DIR" in os.environ:
        fail("unset BELLO_RUNTIME_DIR: this proof covers Bello's default per-user runtime directory")
    phases[arguments.phase]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
