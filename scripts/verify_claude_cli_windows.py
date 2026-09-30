#!/usr/bin/env python3
"""Verify Bello's official Claude Code CLI on a real native Windows runner.

Phases (run in order on a fresh runner with BELLO_RUNTIME_DIR set):

* ``sdk``     - claude-agent-sdk is the pinned release built from its sdist:
                importable, bundled-CLI version 2.1.284, and no bundled claude.exe.
* ``before``  - readiness fails closed with the setup command; doctor warns.
                Nothing is downloaded.
* ``after``   - after ``bello runtime install claude-code``: the verified cache
                is used, its SHA-256 matches the pin, the Windows ACL checks
                pass, Windows reports a valid Authenticode signature by
                Anthropic, and ``claude.exe --version`` reports 2.1.284.
* ``metadata``- a signed-out metadata handshake through Bello's own backend
                (SDK initialize/get_server_info in an isolated CLAUDE_CONFIG_DIR)
                lists claude-sonnet-5-5 with the pinned CLI. No login, no prompt,
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

    from supervisor.runtime.claude_cli import managed_release

    release = managed_release()
    if release is None:
        fail("this runner is not a platform with a managed Claude Code CLI pin")
    version = metadata.version("claude-agent-sdk")
    if version != release.sdk_version or __cli_version__ != release.cli_version:
        fail(f"claude-agent-sdk {version} declares CLI {__cli_version__}; expected {release.sdk_version}/"
             f"{release.cli_version}")
    bundled = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / "claude.exe"
    if bundled.exists():
        fail("the pinned SDK unexpectedly contains a bundled claude.exe; the managed path would be unused")
    ok(f"claude-agent-sdk {version} (sdist build, declares CLI {__cli_version__}) without a bundled CLI")


def check_before() -> None:
    from supervisor import doctor
    from supervisor.appserver import AppServerError
    from supervisor.runtime import claude_cli
    from supervisor.runtime.claude import ClaudeBackend

    def forbidden(*_args, **_kwargs):
        fail("readiness verification attempted a download")

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
    ok("unprepared state fails closed with the setup command; nothing was downloaded")


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
    from supervisor.runtime import claude_cli
    from supervisor.runtime.claude import ClaudeBackend

    cli = ClaudeBackend._official_cli()
    release = claude_cli.managed_release()
    runtime = Path(os.environ["BELLO_RUNTIME_DIR"]).resolve()
    if cli.source != "managed-download" or not cli.path.resolve().is_relative_to(runtime):
        fail("Bello did not use its verified private cache")
    digest = hashlib.sha256(cli.path.read_bytes()).hexdigest()
    if digest != release.sha256 or cli.path.stat().st_size != release.size:
        fail("the cached CLI does not match the pinned official build")
    ok(f"verified {cli.path} (SHA-256 {digest})")
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
    sonnet = by_id.get("claude-sonnet-5-5")
    if sonnet is None:
        fail("the pinned Windows CLI did not advertise claude-sonnet-5-5: " + ", ".join(sorted(by_id)))
    efforts = sonnet["supportedEfforts"]
    if efforts != ["low", "medium", "high", "xhigh", "max"]:
        fail(f"unexpected Sonnet 5.5 efforts {efforts}")
    alias = by_id.get("sonnet", {})
    ok("signed-out metadata lists claude-sonnet-5-5 with efforts " + ", ".join(efforts)
       + (f"; alias sonnet -> {alias.get('resolvedModel')}" if alias else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=["sdk", "before", "after", "metadata"])
    arguments = parser.parse_args()
    if sys.platform != "win32":
        fail("run this verifier on native Windows")
    if not os.environ.get("BELLO_RUNTIME_DIR"):
        fail("set BELLO_RUNTIME_DIR to a runner-private directory")
    {"sdk": check_sdk, "before": check_before, "after": check_after, "metadata": check_metadata}[arguments.phase]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
