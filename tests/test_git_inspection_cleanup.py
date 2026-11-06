"""No-model regression for leaked inspection children on timeout/cancellation."""
import asyncio
import sys

import pytest

from supervisor.controller import BelloController
from supervisor.controller_parts import evidence


def controller(tmp_path):
    result = BelloController.__new__(BelloController)
    result.project_root = tmp_path
    result.task_path = tmp_path / "TASK.md"
    return result


async def test_timeout_kills_drains_and_reaps_child(tmp_path, monkeypatch):
    real_wait_for = asyncio.wait_for
    children = []
    timeouts = []

    async def spawn(*args, **kwargs):
        child = await asyncio.create_subprocess_exec(*args, **kwargs)
        children.append(child)
        return child

    async def quick_wait(awaitable, timeout):
        # _git_output swallows exceptions; record instead of asserting here.
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(evidence, "create_subprocess_exec", spawn)
    monkeypatch.setattr(evidence.compat.asyncio, "wait_for", quick_wait)
    try:
        for _ in range(3):
            result = await controller(tmp_path)._git_output([
                sys.executable, "-c",
                "import os,time; os.write(1,b'x'*200000); time.sleep(60)",
            ])
            assert result is None
            assert children[-1].returncode is not None
        assert timeouts == [5, 5, 5]
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@pytest.mark.parametrize("during_spawn", [False, True])
async def test_cancellation_reaps_child_and_still_propagates(tmp_path, monkeypatch, during_spawn):
    child_created = asyncio.Event()
    release_spawn = asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        child = await asyncio.create_subprocess_exec(*args, **kwargs)
        children.append(child)
        child_created.set()
        if during_spawn:
            await release_spawn.wait()
        return child

    monkeypatch.setattr(evidence, "create_subprocess_exec", spawn)
    task = asyncio.create_task(controller(tmp_path)._git_output([
        sys.executable, "-c", "import time; time.sleep(60)",
    ]))
    try:
        await child_created.wait()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    finally:
        release_spawn.set()
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()


@pytest.mark.parametrize("exit_code,expected", [(0, "ok"), (1, None)])
async def test_normal_completion_semantics_unchanged(tmp_path, exit_code, expected):
    assert await controller(tmp_path)._git_output([
        sys.executable, "-c", f"import sys; print('ok'); sys.exit({exit_code})",
    ]) == expected


async def test_spawn_failure_still_returns_none(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise FileNotFoundError("no executable")
    monkeypatch.setattr(evidence, "create_subprocess_exec", fail)
    assert await controller(tmp_path)._git_output(["missing-binary"]) is None
