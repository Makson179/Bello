"""A restored run continues only with the exact execution launchers it recorded.

Preflight used to skip this comparison: the settings port did not expose the
durable run, so ``getattr(..., "_durable_run", None)`` was always None. Offline;
engines are fakes whose command is the local Python interpreter.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.controller_recovery import DurableRun, RecoveryBlocked, _engine_identity
from tests.test_runtime_preflight import controller_for


class _Launcher:
    def __init__(self, *command: str) -> None:
        self.command = [sys.executable, *command]


class _Engines(SimpleNamespace):
    """Stands in for RuntimeClient: model/list starts some engines, _engine the rest."""

    def __init__(self, base, launchers: dict[str, _Launcher], *, listed: set[str]) -> None:
        super().__init__(**vars(base))
        self._launchers = launchers
        self._engines: dict[str, _Launcher] = {}
        self.loaded: list[str] = []
        self._listed = listed
        self._base_model_list = base.model_list

    async def model_list(self):
        for name in sorted(self._listed):
            self._engines.setdefault(name, self._launchers[name])
        return await self._base_model_list()

    async def _engine(self, name: str) -> _Launcher:
        self.loaded.append(name)
        return self._engines.setdefault(name, self._launchers[name])


def _restored(controller, expected):
    owner = DurableRun.__new__(DurableRun)
    owner.controller, owner.engine_file_cache, owner.expected_engines = controller, {}, expected
    controller._durable_run = owner
    return owner


@pytest.mark.asyncio
async def test_preflight_loads_every_recorded_launcher_before_verifying(tmp_path, monkeypatch):
    controller, events = controller_for(tmp_path, monkeypatch, completion=True)
    order = []
    controller.client._engine = AsyncMock(side_effect=lambda name: order.append(("load", name)))
    controller._durable_run = SimpleNamespace(
        expected_engines={"codex": {}, "claude-code": {}},
        verify_engine_selection=Mock(side_effect=lambda: order.append(("verify",))),
    )
    await controller._runtime_preflight()
    assert order == [("load", "codex"), ("load", "claude-code"), ("verify",)]
    assert "sandbox" in events


@pytest.mark.asyncio
async def test_changed_launcher_blocks_before_any_paid_self_test(tmp_path, monkeypatch):
    controller, events = controller_for(tmp_path, monkeypatch, completion=True)
    controller.client._engine = AsyncMock()
    controller._durable_run = SimpleNamespace(
        expected_engines={"codex": {}},
        verify_engine_selection=Mock(side_effect=RecoveryBlocked("selected execution engine changed")),
    )
    with pytest.raises(RecoveryBlocked, match="selected execution engine changed"):
        await controller._runtime_preflight()
    assert "paid-self-test" not in events and "triage" not in events and "sandbox" not in events


@pytest.mark.asyncio
async def test_real_binding_covers_an_engine_a_fresh_preflight_loads_later(tmp_path, monkeypatch):
    # The original run loaded "codex" only later (e.g. cheap triage); model/list
    # of the restored run starts only "claude-code".
    launchers = {"claude-code": _Launcher("-c", "pass"), "codex": _Launcher("-c", "pass  # triage")}
    original = SimpleNamespace(_engines=dict(launchers))
    expected = _engine_identity(original, {})
    controller, _ = controller_for(tmp_path, monkeypatch, completion=True)
    controller.client = _Engines(controller.client, launchers, listed={"claude-code"})
    owner = _restored(controller, expected)
    # Without loading first, the live set would miss the recorded engine.
    assert _engine_identity(SimpleNamespace(_engines={"claude-code": launchers["claude-code"]}), {}) != expected
    await controller._runtime_preflight()
    assert controller.client.loaded == ["claude-code", "codex"] and owner.expected_engines == expected


@pytest.mark.asyncio
async def test_real_binding_rejects_a_changed_launcher(tmp_path, monkeypatch):
    recorded = {"claude-code": _Launcher("-c", "pass")}
    expected = _engine_identity(SimpleNamespace(_engines=dict(recorded)), {})
    controller, events = controller_for(tmp_path, monkeypatch, completion=True)
    controller.client = _Engines(controller.client, {"claude-code": _Launcher("-c", "pass  # changed")},
                                 listed={"claude-code"})
    _restored(controller, expected)
    with pytest.raises(RecoveryBlocked, match="selected execution engine changed"):
        await controller._runtime_preflight()
    assert "paid-self-test" not in events


@pytest.mark.asyncio
async def test_fresh_run_has_no_recorded_launchers_to_bind(tmp_path, monkeypatch):
    controller, events = controller_for(tmp_path, monkeypatch, completion=True)
    controller.client._engine = AsyncMock()
    _restored(controller, None)
    await controller._runtime_preflight()
    controller.client._engine.assert_not_awaited()
    assert "sandbox" in events
