"""Native Windows enforcement/timing smoke used under a standard-user account."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time


def sddl(path: Path) -> str:
    escaped = str(path).replace("'", "''")
    return subprocess.check_output(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Acl -LiteralPath '{escaped}').Sddl",
        ],
        text=True,
        encoding="utf-8",
    ).strip()


HOST_ENV_SECRET = "bello-native-parent-env-must-not-leak"


def request(
    root: Path,
    command: str,
    *,
    mode: str = "workspace-write",
    readable_roots: tuple[Path, ...] = (),
) -> dict[str, object]:
    return {
        "operation": "run",
        "protocolVersion": 1,
        "command": command,
        "cwd": str(root),
        "root": str(root),
        "mode": mode,
        "readableRoots": [str(path) for path in readable_roots],
        "privatePaths": [str(root / ".supervisor"), str(root / ".codex" / "bello-run")],
        "networkAccess": False,
    }


def invoke(
    helper: Path,
    payload: dict[str, object],
    *,
    close_on_ready: bool = False,
) -> tuple[bytes, int]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    process = subprocess.Popen(
        [str(helper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"BELLO_CI_HOST_SECRET": HOST_ENV_SECRET},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = bytearray()
    control = bytearray()
    ready = threading.Event()

    def read_output() -> None:
        while chunk := process.stdout.read1(4096):
            output.extend(chunk)
            if b"READY" in output:
                ready.set()

    def read_control() -> None:
        while chunk := process.stderr.read1(4096):
            control.extend(chunk)

    threads = [
        threading.Thread(target=read_output, daemon=True),
        threading.Thread(target=read_control, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        process.stdin.write(struct.pack("<I", len(body)) + body)
        process.stdin.flush()
        if close_on_ready:
            if not ready.wait(120):
                raise AssertionError("sandbox child did not reach its cancellation marker")
            process.stdin.close()
        process.wait(timeout=300)
        if not close_on_ready:
            process.stdin.close()
        for thread in threads:
            thread.join(timeout=10)
            if thread.is_alive():
                raise AssertionError("sandbox helper pipe did not close")
        if len(control) > 64 * 1024 or control.count(b"\n") != 1:
            raise AssertionError(f"invalid helper control channel: {bytes(control[:1000])!r}")
        terminal = json.loads(control.decode("utf-8"))
        if process.returncode != 0 or terminal.get("kind") != "exit":
            raise AssertionError(
                f"sandbox backend failed: helper={process.returncode}, terminal={terminal!r}, "
                f"output={bytes(output[-2000:])!r}"
            )
        return bytes(output), int(terminal["exitCode"])
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=10)
        for stream in (process.stdout, process.stderr):
            stream.close()
        for thread in threads:
            thread.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--node-source", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    helper = arguments.helper.resolve(strict=True)
    node_source = arguments.node_source.resolve(strict=True)
    status_process = subprocess.run(
        [str(helper), "host-status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if status_process.returncode != 0:
        raise AssertionError(f"non-admin host status failed: {status_process.stderr!r}")
    host_status = json.loads(status_process.stdout)
    if host_status.get("kind") != "hostPreparation" or host_status.get("prepared") is not True:
        raise AssertionError(f"host is not prepared for the non-admin smoke: {host_status!r}")
    system_root = str(host_status["systemRoot"])
    unauthorized_prepare = subprocess.run(
        [str(helper), "host-prepare"],
        capture_output=True,
        timeout=30,
    )
    if unauthorized_prepare.returncode == 0:
        raise AssertionError("host preparation unexpectedly accepted a non-admin caller")
    base = Path(tempfile.mkdtemp(prefix="bello-native-standard-user-")).resolve()
    root = base / "workspace"
    root.mkdir()
    private = root / ".supervisor"
    private.mkdir()
    (private / "token.txt").write_text("never-disclose", encoding="utf-8")
    bello_state = root / ".codex" / "bello-run"
    bello_state.mkdir(parents=True)
    (bello_state / "state.txt").write_text("private-state", encoding="utf-8")
    outside = base / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    ordinary = root / "ordinary.txt"
    ordinary.write_text("ordinary", encoding="utf-8")
    original_sddl = {path: sddl(path) for path in (root, ordinary, private)}

    profile_secret = Path.home() / "bello-native-profile-secret.txt"
    profile_secret.write_text("profile-secret-must-not-leak", encoding="utf-8")

    output, code = invoke(helper, request(root, "echo STANDARD_USER_OK>allowed.txt"))
    if code != 0 or (root / "allowed.txt").read_text().strip() != "STANDARD_USER_OK":
        raise AssertionError(f"workspace write failed: code={code}, output={output!r}")

    read_only_target = root / "read-only-write-must-fail.txt"
    output, code = invoke(
        helper,
        request(
            root,
            "echo WRITE_ESCAPE>read-only-write-must-fail.txt",
            mode="read-only",
        ),
    )
    if code == 0 or read_only_target.exists():
        raise AssertionError(f"read-only workspace accepted a write: {output!r}")

    for forbidden in (private / "token.txt", outside, profile_secret):
        output, code = invoke(helper, request(root, f'type "{forbidden}"'))
        if (
            code == 0
            or b"never-disclose" in output
            or b"outside" in output
            or b"profile-secret-must-not-leak" in output
        ):
            raise AssertionError(f"sandbox disclosed {forbidden}: {output!r}")

    output, code = invoke(
        helper,
        request(
            root,
            "if defined BELLO_CI_HOST_SECRET (echo %BELLO_CI_HOST_SECRET% & exit /b 0) "
            "else exit /b 7",
        ),
    )
    if code != 7 or HOST_ENV_SECRET.encode() in output:
        raise AssertionError(f"parent environment secret reached the child: {output!r}")

    toolchain = base / "staged-node-toolchain"
    toolchain.mkdir()
    staged_node = toolchain / "node.exe"
    shutil.copy2(node_source, staged_node)
    node_script = root / "node-smoke.js"
    node_marker = root / "node-toolchain-ok.txt"
    node_script.write_text(
        "require('fs').writeFileSync('node-toolchain-ok.txt', 'STAGED_NODE_OK')\n",
        encoding="utf-8",
    )
    output, code = invoke(
        helper,
        request(
            root,
            f'"{staged_node}" "{node_script}"',
            readable_roots=(toolchain,),
        ),
    )
    if code != 0 or node_marker.read_text(encoding="utf-8") != "STAGED_NODE_OK":
        raise AssertionError(f"staged per-user Node toolchain failed: {output!r}")

    metadata_script = root / "root-metadata-only.js"
    metadata_script.write_text(
        "const fs=require('fs');\n"
        f"const root={json.dumps(system_root)};\n"
        "if(!fs.lstatSync(root).isDirectory())process.exit(91);\n"
        "try { fs.readdirSync(root); process.exit(92); }\n"
        "catch(e) { if(e.code==='EACCES'||e.code==='EPERM')process.exit(23); throw e; }\n",
        encoding="utf-8",
    )
    output, code = invoke(
        helper,
        request(
            root,
            f'"{staged_node}" "{metadata_script}"',
            readable_roots=(toolchain,),
        ),
    )
    if code != 23:
        raise AssertionError(
            "system-root metadata setup must allow lstat but not directory listing: "
            f"exit={code}, output={output!r}"
        )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        network_script = root / "network-deny.js"
        network_script.write_text(
            "const net=require('net');\n"
            f"const socket=net.createConnection({{host:'127.0.0.1',port:{port}}});\n"
            "socket.setTimeout(5000,()=>{socket.destroy();process.exit(23)});\n"
            "socket.on('connect',()=>process.exit(0));\n"
            "socket.on('error',()=>process.exit(23));\n",
            encoding="utf-8",
        )
        output, code = invoke(
            helper,
            request(
                root,
                f'"{staged_node}" "{network_script}"',
                readable_roots=(toolchain,),
            ),
        )
        if code != 23:
            raise AssertionError(
                "networkAccess=false did not produce the staged probe's denied result: "
                f"exit={code}, output={output!r}"
            )
    finally:
        listener.close()

    output, code = invoke(helper, request(root, "ren .codex escaped-codex"))
    if code == 0 or not (root / ".codex").is_dir() or (root / "escaped-codex").exists():
        raise AssertionError(f"private ancestor rename was not blocked: {output!r}")
    output, code = invoke(
        helper,
        request(root, r"mklink /H exposed-state.txt .codex\bello-run\state.txt"),
    )
    if code == 0 or (root / "exposed-state.txt").exists():
        raise AssertionError(f"private hard-link alias was not blocked: {output!r}")

    marker = root / "descendant-survived.txt"
    cancellation = (
        "echo READY& start \"\" /b cmd.exe /d /s /c "
        f'"ping -n 4 127.0.0.1 >nul& echo escaped>{marker}"'
        "& ping -n 30 127.0.0.1 >nul"
    )
    _, code = invoke(helper, request(root, cancellation), close_on_ready=True)
    time.sleep(4)
    if code != 130 or marker.exists():
        raise AssertionError(
            f"Job cancellation failed: exit={code}, descendant_marker={marker.exists()}"
        )

    large = root / "representative-tree"
    large.mkdir()
    object_count = 4_000
    for index in range(object_count):
        bucket = large / f"d{index // 200:03d}"
        bucket.mkdir(exist_ok=True)
        (bucket / f"f{index:05d}.txt").write_text("fixture", encoding="utf-8")
    started = time.perf_counter()
    output, code = invoke(helper, request(root, "echo LARGE_TREE_OK"))
    duration = time.perf_counter() - started
    if code != 0 or b"LARGE_TREE_OK" not in output:
        raise AssertionError(f"large-tree run failed: code={code}, output={output!r}")
    restored_sddl = {path: sddl(path) for path in original_sddl}
    changed = {
        str(path): {"before": original_sddl[path], "after": restored_sddl[path]}
        for path in original_sddl
        if original_sddl[path] != restored_sddl[path]
    }
    if changed:
        raise AssertionError(f"sandbox did not restore representative DACL SDDL: {changed!r}")
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runner": platform.platform(),
                "standardUser": True,
                "hostPrepared": True,
                "systemRootListingDenied": True,
                "mode": "workspace-write",
                "fileCount": object_count,
                "directoryCount": 21,
                "durationSeconds": duration,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    profile_secret.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
