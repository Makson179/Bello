#!/usr/bin/env python3
"""Final-wheel default native helper HTTPS/cache/capability smoke; no model calls.

Imports supervisor only from the active venv. Does not modify bundle tables,
downloaders or provider/auth routing. Direct helper capability discovery is NOT
an ordinary RuntimeClient turn or a functional NNC/batching proof.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sys
from urllib.parse import urlsplit
from zipfile import ZipFile


def require(value, message: str) -> None:
    if not value:
        raise ValueError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_ledger(value: dict) -> None:
    require(value.get("schema") == "bello.installed-wheel-delivery-ledger.v1", "Wrong ledger schema")
    for key in ("wheel_sha256",):
        require(re.fullmatch(r"[a-f0-9]{64}", str(value.get(key, ""))), "Invalid ledger hash")
    for key in ("product_source_sha", "helper_source_sha"):
        require(re.fullmatch(r"[a-f0-9]{40}", str(value.get(key, ""))), "Invalid source identity")
    require(value.get("target") in {"linux-x64", "windows-x64", "darwin-arm64"}, "Unsupported target")
    bundle = value.get("bundle", {})
    url = urlsplit(bundle.get("url", ""))
    require(url.scheme == "https" and url.netloc == "github.com"
            and url.path.startswith("/Makson179/Bello/releases/download/")
            and "/local-unpublished/" not in url.path and not url.query and not url.fragment,
            "Ledger does not identify a public helper release URL")
    for key in ("archive_sha256", "manifest_sha256"):
        require(re.fullmatch(r"[a-f0-9]{64}", str(bundle.get(key, ""))), "Invalid bundle hash")
    require(isinstance(value.get("files"), dict) and value["files"], "Missing helper file ledger")
    for name, checksum in value["files"].items():
        require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
                and "\\" not in name and re.fullmatch(r"[a-f0-9]{64}", str(checksum)), "Invalid helper file identity")


def wheel_payload(wheel: Path) -> dict[str, str]:
    result = {}
    with ZipFile(wheel) as archive:
        for info in archive.infolist():
            # Windows ZipInfo normalizes backslashes in filename. Validate the
            # original archive spelling so host normalization cannot hide it.
            name = info.orig_filename
            require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
                    and "\\" not in name, "Unsafe wheel member")
            if info.is_dir():
                continue
            for marker in (".data/purelib/", ".data/platlib/"):
                if marker in name:
                    name = name.split(marker, 1)[1]
                    break
            if name.startswith("supervisor/"):
                require(name not in result, "Duplicate wheel package member")
                result[name] = hashlib.sha256(archive.read(info)).hexdigest()
    require("supervisor/runtime/native_codex_install.py" in result, "Wheel has no native installer")
    return result


def clean_environment(home: Path, runtime: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME")
           if os.environ.get(key)}
    for name in ("tmp", "config", "data", "cache", "codex"):
        (home / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / "codex"),
               BELLO_RUNTIME_DIR=str(runtime), TMPDIR=str(home / "tmp"), TMP=str(home / "tmp"),
               TEMP=str(home / "tmp"), APPDATA=str(home / "config"), LOCALAPPDATA=str(home / "data"),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), PATH=os.environ.get("PATH", os.defpath),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHONNOUSERSITE="1",
               LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    return env


def require_default_environment(environment: dict[str, str]) -> None:
    for name in ("BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST"):
        require(not environment.get(name, "").strip(), "Explicit helper override supplied: " + name)


def imported_modules(package_parent: Path, expected: dict[str, str]) -> dict:
    rows = {}
    for name, module in tuple(sys.modules.items()):
        if name != "supervisor" and not name.startswith("supervisor."):
            continue
        path = Path(module.__file__).resolve(strict=True)
        require(path.is_relative_to(package_parent), "Supervisor module imported outside installed wheel")
        relative = path.relative_to(package_parent).as_posix()
        require(relative in expected and digest(path) == expected[relative], "Imported module differs from wheel")
        rows[name] = {"path": str(path), "sha256": expected[relative]}
    return rows


def snapshot(directory: Path, files: dict[str, str]) -> dict:
    result = {}
    for name, expected in files.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink() and digest(path) == expected, "Cache file identity mismatch")
        stat = path.stat()
        result[name] = {"sha256": expected, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return result


async def capabilities(command: list[str], home: Path, output: Path) -> dict:
    """Same credential-free initialize/list RPC shape as the existing native fixture."""
    native_home = home / "codex"
    (native_home / "config.toml").write_text(
        'cli_auth_credentials_store="file"\ncheck_for_update_on_startup=false\n'
        '[analytics]\nenabled=false\n[feedback]\nenabled=false\n'
        '[features]\nremote_models=false\nplugins=false\nremote_plugin=false\n'
        'recommended_plugins=false\napps=false\n')
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = "http://127.0.0.1:9"
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    result = {"kind": "direct-helper-capability-discovery", "model_turns": 0,
              "ordinary_runtime_client_turn": False, "requested_methods": [], "features": []}
    raw_out = (output / "capability-stdout.raw.jsonl").open("xb")
    raw_err = (output / "capability-stderr.raw.log").open("xb")
    process = None
    stderr_task = None
    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=home, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024)
        async def drain():
            total = 0
            while chunk := await process.stderr.read(65536):
                total += len(chunk)
                if total <= 4 * 1024 * 1024:
                    raw_err.write(chunk)
            return total
        stderr_task = asyncio.create_task(drain())
        counter = 0
        stdout_bytes = 0
        async def send(row):
            process.stdin.write((json.dumps(row) + "\n").encode())
            await process.stdin.drain()
        async def rpc(method, params):
            nonlocal counter, stdout_bytes
            counter += 1
            result["requested_methods"].append(method)
            await send({"id": counter, "method": method, "params": params})
            async with asyncio.timeout(30):
                for _ in range(2000):
                    line = await process.stdout.readline()
                    require(bool(line), "Helper stdout ended before RPC response")
                    stdout_bytes += len(line)
                    require(stdout_bytes <= 8 * 1024 * 1024, "Capability stdout limit exceeded")
                    raw_out.write(line)
                    row = json.loads(line)
                    require(not ("method" in row and "id" in row), "Unexpected helper request")
                    if row.get("id") == counter:
                        require("error" not in row, "Capability RPC failed")
                        return row.get("result", {})
            raise ValueError("Too many capability RPC notifications")
        await rpc("initialize", {"clientInfo": {"name": "bello_installed_wheel_smoke", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        await send({"method": "initialized", "params": {}})
        cursor = None
        seen = set()
        names = set()
        for _ in range(20):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = await rpc("experimentalFeature/list", params)
            names.update(item["name"] for item in response.get("data", []) if isinstance(item, dict) and isinstance(item.get("name"), str))
            cursor = response.get("nextCursor")
            if not cursor:
                break
            require(cursor not in seen, "Repeated feature cursor")
            seen.add(cursor)
        else:
            raise ValueError("Too many feature pages")
        require({"bello_async_tools", "bello_context_compaction"}.issubset(names), "Required native capabilities missing")
        result.update(passed=True, features=sorted(names))
    finally:
        if process is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            result["process_exit_code"] = process.returncode
        if stderr_task:
            result["stderr_bytes"] = await stderr_task
        raw_out.close()
        raw_err.close()
    return result


def run(args) -> dict:
    wheel, ledger_path = args.wheel.resolve(strict=True), args.ledger.resolve(strict=True)
    ledger = json.loads(ledger_path.read_text())
    validate_ledger(ledger)
    require(digest(wheel) == ledger["wheel_sha256"], "Wheel hash differs from ledger")
    require(sys.prefix != sys.base_prefix and sys.flags.isolated, "Run with isolated Python from a fresh venv")
    output = args.output.absolute()
    output.mkdir(parents=False, exist_ok=False)
    report = {"schema": "bello.installed-wheel-default-delivery.v1", "passed": False,
              "scope": "installed-wheel-default-HTTPS-selection-cache-and-helper-capabilities",
              "wheel_sha256": digest(wheel), "ledger_sha256": digest(ledger_path),
              "product_source_sha": ledger["product_source_sha"], "helper_source_sha": ledger["helper_source_sha"],
              "qualification_binding": ledger.get("qualification_binding"),
              "target": ledger["target"], "paid_model_calls": 0, "model_turns": 0,
              "ordinary_runtime_client_turn_proven": False, "phase": "wheel_identity",
              "limitations": ["No ordinary credentialed RuntimeClient/Bello turn; that needs a separately authorized subscription smoke.",
                              "Capability names are not functional NNC or updated-batching evidence; use exact-binary platform qualification receipts.",
                              "Helper process exit only; no arbitrary descendant-cleanup claim."]}
    try:
        require_default_environment(os.environ)
        runtime, home = output / "runtime-cache", output / "isolated-home"
        env = clean_environment(home, runtime)
        os.environ.clear()
        os.environ.update(env)
        os.chdir(home)
        expected = wheel_payload(wheel)
        package = importlib.import_module("supervisor")
        package_parent = Path(package.__file__).resolve(strict=True).parent.parent
        require(package_parent.is_relative_to(Path(sys.prefix).resolve()), "Package is not in active venv")
        require(importlib.metadata.version("Bello") == "0.8.0", "Wrong installed Bello version")
        for name, checksum in expected.items():
            path = package_parent / name
            require(path.is_file() and digest(path) == checksum, "Installed package bytes differ from wheel")
        installer = importlib.import_module("supervisor.runtime.native_codex_install")
        report["installed_modules_before_network"] = imported_modules(package_parent, expected)
        key = installer._platform_key()
        targets = {("Linux", "x86_64"): "linux-x64", ("Windows", "x86_64"): "windows-x64", ("Darwin", "arm64"): "darwin-arm64"}
        require(targets.get(key) == ledger["target"], "Ledger does not match native platform")
        spec = installer.ASYNC_BUNDLES.get(key)
        require(spec is not None and installer.BUNDLES.get(key) == spec, "Default helper tables disagree")
        spec_values = {name: getattr(spec, name) for name in ("url", "archive_sha256", "manifest_sha256")}
        require(spec_values == ledger["bundle"], "Final wheel default pins differ from release ledger")
        require(not runtime.exists(), "Runtime cache is not fresh")
        report.update(version="0.8.0", python=sys.version, platform=platform.platform(),
                      package_parent=str(package_parent), package_files_verified=len(expected), bundle=spec_values)
        requests = []
        def audit(event, values):
            if event == "urllib.Request":
                url = str(values[0])
                requests.append({"url_sha256": hashlib.sha256(url.encode()).hexdigest(),
                                 "host": urlsplit(url).hostname, "is_pinned_url": url == spec.url})
        sys.addaudithook(audit)
        report["phase"] = "cold_https_install"
        command, manifest = installer.ensure_native_async()
        require(manifest is not None, "Default helper manifest missing")
        directory = runtime / "native-codex" / spec.archive_sha256
        require(manifest.resolve() == (directory / "selection-manifest.json").resolve(), "Unexpected cache location")
        require(digest(manifest) == spec.manifest_sha256, "Manifest differs from promoted artifact")
        manifest_data = json.loads(manifest.read_text())
        require(manifest_data["files"] == ledger["files"], "Cache payload differs from qualified ledger")
        require(any(item["is_pinned_url"] for item in requests), "No default pinned HTTPS request observed")
        require(command[1:] == ["app-server", "--listen", "stdio://"], "Unexpected default native command shape")
        expected_binary = directory / ("bin/codex.exe" if os.name == "nt" else "bin/codex")
        require(Path(command[0]).resolve() == expected_binary.resolve(), "Default command selects a different binary")
        complete_files = {**ledger["files"], "selection-manifest.json": spec.manifest_sha256}
        before = snapshot(directory, complete_files)
        first_requests = len(requests)
        report["phase"] = "cache_reuse"
        require(installer.ensure_native_async() == (command, manifest)
                and installer.ensure_native_selection() == (command, manifest), "Cache selectors changed")
        require(len(requests) == first_requests, "Cache reuse made another urllib request")
        require(snapshot(directory, complete_files) == before, "Cache changed on reuse")
        report.update(cold_cache=True, reused_cache=True, unchanged_cache=True,
                      default_downloader_unmodified=True, default_tables_unmodified=True,
                      explicit_binary_override=False, distribution_source="production-https",
                      urllib_requests=requests, binary_sha256=digest(expected_binary), cache_files=before)
        report["phase"] = "helper_capabilities"
        report["capabilities"] = asyncio.run(capabilities(command, home, output))
        require(snapshot(directory, complete_files) == before, "Helper capability discovery changed cache")
        report["installed_modules"] = imported_modules(package_parent, expected)
        report.update(passed=True, phase="complete")
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:800])
    finally:
        (output / "RESULT.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    result = run(parser.parse_args())
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
