"""A real terminal must still reach a guarded worker and preserve Ctrl-C."""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX controlling-terminal regression")


@pytest.mark.parametrize("interrupt", [False, True])
def test_guardian_preserves_terminal_input_and_graceful_interrupt(tmp_path, interrupt):
    import fcntl
    import pty
    import termios

    worker = tmp_path / "worker.py"
    worker.write_text(
        "from supervisor.process_fence import configure_worker\n"
        "import sys,time\n"
        "configure_worker()\n"
        "try:\n"
        "    print('FIXTURE_READY', flush=True)\n"
        "    value = input()\n"
        "    assert value == 'hello-from-terminal'\n"
        "    print('FIXTURE_INPUT_RECEIVED', flush=True)\n"
        + ("    time.sleep(30)\n" if interrupt else "")
        + "except KeyboardInterrupt:\n"
        "    print('FIXTURE_GRACEFUL_INTERRUPT', flush=True)\n"
        "    sys.exit(130)\n"
    )
    source = str(Path(__file__).resolve().parents[1])
    code = (
        "from pathlib import Path; from supervisor.watchdog import watch_command; import sys; "
        f"sys.exit(watch_command([sys.executable, {str(worker)!r}], project_root=Path({str(tmp_path)!r})))"
    )
    master, slave = pty.openpty()

    def claim_terminal():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    environment = {**os.environ, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1"}
    for key in tuple(environment):
        if key.startswith(("BELLO_WATCHDOG_", "BELLO_FENCE_", "BELLO_GUARDIAN_", "BELLO_RECOVERY_")):
            environment.pop(key)
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code], cwd=tmp_path, env=environment,
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        preexec_fn=claim_terminal,
    )
    os.close(slave)
    output = bytearray()

    def until(marker):
        deadline = time.monotonic() + 12
        while marker not in output:
            assert time.monotonic() < deadline, "guarded terminal did not respond"
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    pytest.fail("guarded terminal exited before its expected fixture marker")
                output.extend(chunk)
            assert len(output) < 1024 * 1024, "unexpected unbounded fixture output"

    try:
        until(b"FIXTURE_READY")
        os.write(master, b"hello-from-terminal\n")
        until(b"FIXTURE_INPUT_RECEIVED")
        if interrupt:
            os.write(master, b"\x03")
            until(b"FIXTURE_GRACEFUL_INTERRUPT")
        assert process.wait(timeout=15) == (130 if interrupt else 0)
        assert output.count(b"FIXTURE_READY") == 1
        assert b"restoring the same run" not in output
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
