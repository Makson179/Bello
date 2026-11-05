"""Regression: metadata-only review work must not starve provider control IO."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import supervisor.controller as controller_module
from supervisor.controller import BelloController


def controller_with_status(tmp_path, monkeypatch, count):
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = tmp_path / 'TASK.md'
    controller.use_git_diff = True
    controller.observed_changed_files = {}
    status = ''.join(f'?? snapshots/{i}.py\0' for i in range(count))
    async def git_output(command):
        if command[1] == 'rev-parse':
            return 'true'
        return status if command[1] == 'status' else ''
    monkeypatch.setattr(controller, '_git_output', git_output)
    return controller


async def test_large_discovery_delivers_control_reply_before_timeout(tmp_path, monkeypatch):
    controller = controller_with_status(tmp_path, monkeypatch, 192)
    original = controller_module._is_ignored_changed_path
    checks = 0
    reply_seen = []
    def slow_check(*args, **kwargs):
        nonlocal checks
        checks += 1
        time.sleep(0.001)
        return original(*args, **kwargs)
    monkeypatch.setattr(controller_module, '_is_ignored_changed_path', slow_check)
    async def control_reply():
        await asyncio.sleep(0)
        reply_seen.append(checks)
    reply = asyncio.create_task(control_reply())
    files = await controller.changed_files()
    await reply
    assert 0 < reply_seen[0] < 192
    assert reply_seen[0] <= 64
    assert len(files) == checks == 192
    assert files[0].path == 'snapshots/0.py'
    assert files[-1].path == 'snapshots/191.py'


async def test_large_discovery_is_cancellable_without_state_changes(tmp_path, monkeypatch):
    controller = controller_with_status(tmp_path, monkeypatch, 192)
    discovery = asyncio.create_task(controller.changed_files())
    await asyncio.sleep(0)
    discovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await discovery
    assert controller.observed_changed_files == {}


async def test_privacy_cache_and_overflow_semantics_unchanged(tmp_path, monkeypatch):
    controller = controller_with_status(tmp_path, monkeypatch, 513)
    controller._coder_snapshot = SimpleNamespace(plan_relative_path='snapshots/1.py')
    files = await controller.changed_files()
    assert len(files) == 500
    assert len(files.omitted) == 12
    assert 'snapshots/1.py' not in {f.path for f in [*files, *files.omitted]}
