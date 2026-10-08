"""No-provider subprocess fixture for process-watchdog integration tests."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from supervisor.process_fence import configure_worker, create_subprocess_exec, process_group_id, signal_process_group


async def main() -> None:
    configure_worker()
    root = Path(sys.argv[1])
    mode = sys.argv[2]
    path = root / "fixture-state.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    attempt = previous.get("attempt", 0) + 1
    record = {
        "run_id": previous.get("run_id", "fixture-run"), "attempt": attempt,
        "owner_pid": os.getpid(), "owner_epoch": os.environ["BELLO_WATCHDOG_FENCE_TOKEN"],
        "eligible": mode not in {"terminal", "blocked"}, "terminal": mode == "terminal",
        "reason": "fixture blocked" if mode == "blocked" else "eligible local fixture",
        "thread_id": previous.get("thread_id", "same-fixture-thread"),
        "budget_consumed": previous.get("budget_consumed", 0) + 1,
    }
    if attempt > 1:
        receipt = json.loads((root / ".supervisor/controller/fence-receipt.json").read_text())
        assert receipt["nonce"] == os.environ["BELLO_RECOVERY_PERMIT"]
        assert receipt["owner_pid"] == previous["owner_pid"]
        assert receipt["owner_epoch"] == previous["owner_epoch"]
        assert receipt["run_id"] == record["run_id"]
    path.write_text(json.dumps(record))
    if mode == "leases":
        old = None
        for _ in range(12):
            process = await create_subprocess_exec(sys.executable, "-c", "pass", start_new_session=True)
            await process.wait()
            handle = process_group_id(process)
            signal_process_group(handle, signal.SIGKILL)
            old = old or handle
        process = await create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(300)", start_new_session=True)
        signal_process_group(old, signal.SIGKILL)
        await asyncio.sleep(0.05)
        assert process.returncode is None, "a retired lease signalled a replacement group"
        signal_process_group(process_group_id(process), signal.SIGKILL)
        await process.wait()
        rows = subprocess.check_output(["/bin/ps", "-axo", "ppid=,stat="], text=True)
        children = [row.split() for row in rows.splitlines() if row.split() and int(row.split()[0]) == os.getppid()]
        (root / "lease-evidence.json").write_text(json.dumps({"guardian_children": len(children), "zombies": sum(row[1].startswith("Z") for row in children)}))
        return
    if mode in {"child", "wait", "detached"}:
        child_code = (
            "import os,time,pathlib; "
            f"pathlib.Path({str(root / 'child.pid')!r}).write_text(str(os.getpid())); "
            "time.sleep(300)"
        )
        if mode == "detached":
            # This grandchild creates a session below the Python launch helper.
            child_code = (
                "import os,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=os.name != 'nt'); "
                "time.sleep(300)"
            )
        await create_subprocess_exec(sys.executable, "-c", child_code, start_new_session=os.name != "nt")
        deadline = time.monotonic() + 5
        while not (root / "child.pid").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        (root / "ready").write_text("ready")
    if mode == "wait":
        try:
            await asyncio.sleep(300)
        finally:
            (root / "graceful-stop").write_text("controller cleanup completed")
    if mode == "success" or (mode in {"once", "child", "detached"} and attempt > 1):
        return
    if mode == "provider":
        raise SystemExit(2)
    if mode == "interrupt":
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(1)
    os._exit(86)


if __name__ == "__main__":
    asyncio.run(main())
