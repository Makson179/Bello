from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from click.testing import CliRunner
import pytest

from supervisor.appserver import AppServerError
from supervisor.main import cli
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.pi import pi_agent_directory


@pytest.mark.parametrize(("env", "suffix"), [
    ({"BELLO_PI_AGENT_DIR": "bello", "PI_CODING_AGENT_DIR": "pi"}, "bello"),
    ({"PI_CODING_AGENT_DIR": "pi"}, "pi"),
    ({"BELLO_PI_AGENT_DIR": "", "PI_CODING_AGENT_DIR": "pi"}, "pi"),
    ({"BELLO_PI_AGENT_DIR": "", "PI_CODING_AGENT_DIR": ""}, ".pi/agent"),
    ({}, ".pi/agent"),
    ({"BELLO_PI_AGENT_DIR": "~/custom"}, "custom"),
    ({"BELLO_PI_AGENT_DIR": "~"}, "."),
    ({"PI_CODING_AGENT_DIR": "nested/../pi"}, "pi"),
])
def test_python_and_node_directory_precedence_match(tmp_path, env, suffix):
    expected = tmp_path / suffix
    actual = pi_agent_directory(env=env, cwd=tmp_path, home=tmp_path)
    assert actual == Path(os.path.abspath(expected))
    assert list(tmp_path.iterdir()) == []
    node = shutil.which(os.environ.get("BELLO_NODE", "node"))
    if not node:
        pytest.skip("cross-language directory regression requires Node.js")
    module = Path(__file__).resolve().parents[1] / "supervisor/pi_worker/src/agent-directory.mjs"
    script = f"""
import {{ piAgentDirectory }} from {json.dumps(module.as_uri())};
const input = JSON.parse(process.argv[1]);
process.stdout.write(piAgentDirectory(input));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script,
        json.dumps({"env": env, "cwd": str(tmp_path), "home": str(tmp_path)})],
        capture_output=True, text=True, check=True, timeout=10)
    assert Path(result.stdout) == actual


@pytest.mark.parametrize("source", ["bello", "pi", "empty-bello", "default"])
async def test_catalog_and_runtime_initialize_the_same_selected_directory(monkeypatch, tmp_path, source):
    calls = []
    monkeypatch.delenv("BELLO_PI_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    expected = tmp_path / "home/.pi/agent"
    if source != "default":
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
        expected = tmp_path / "pi"
    if source == "bello":
        monkeypatch.setenv("BELLO_PI_AGENT_DIR", str(tmp_path / "bello"))
        expected = tmp_path / "bello"
    elif source == "empty-bello":
        monkeypatch.setenv("BELLO_PI_AGENT_DIR", "")

    class Backend:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def request(self, method, params, **kwargs):
            calls.append((method, params))
            return {"data": []}

        async def stop(self):
            pass

    monkeypatch.setattr("supervisor.runtime.install.worker_command", lambda: ["node", str(tmp_path / "worker.mjs")])
    monkeypatch.setattr("supervisor.runtime.client.WorkerTransport", Backend)
    client = RuntimeClient(cwd=tmp_path, state_dir=tmp_path / "state")
    try:
        await client.request("model/list", {"engines": ["pi"]})
        assert calls[0] == ("initialize", {"stateDir": str(tmp_path / "state/pi"), "agentDir": str(expected)})
        assert await client._engine("pi") is client._engines["pi"]
        assert sum(method == "initialize" for method, _ in calls) == 1
        assert not expected.exists()
    finally:
        await client.stop()


async def test_refresh_preserves_pi_freshness_and_subscription_filter(tmp_path):
    calls = []
    freshness = {"source": "local", "loadedAt": "2026-10-06T00:00:00Z",
                 "refreshPolicy": "explicit-local", "networkAllowed": False, "remoteFreshness": "unknown"}

    class Backend:
        async def request(self, method, params, **kwargs):
            calls.append((method, params))
            return {"catalogFreshness": freshness, "catalogError": "secret upstream error", "data": [
                {"id": "gpt-example", "qualifiedId": "openai/gpt-example", "provider": "openai"},
                {"id": "gpt-example", "qualifiedId": "openai-codex/gpt-example", "provider": "openai-codex"},
            ]}

        async def stop(self):
            pass

    client = RuntimeClient(cwd=tmp_path, backends={"pi": Backend()})
    try:
        result = await client.request("model/list", {"engines": ["pi"], "refresh": True})
        assert calls == [("model/list", {"refresh": True})]
        assert result["catalogFreshness"] == {"pi": freshness}
        assert set(result["catalogErrors"]) == {"pi"}
        assert "secret upstream error" not in json.dumps(result)
        assert {item["provider"] for item in result["data"]} == {"openai"}
    finally:
        await client.stop()


@pytest.mark.parametrize("params", [
    {"engines": ["pi"], "refresh": "true"},
    {"engines": ["pi", "codex"], "refresh": True},
    {"engines": ["codex"], "refresh": True},
])
async def test_refresh_rejects_ambiguous_scope_before_engine_start(tmp_path, params):
    client = RuntimeClient(cwd=tmp_path)
    try:
        with pytest.raises(AppServerError, match="refresh"):
            await client.request("model/list", params)
        assert client._engines == {}
    finally:
        await client.stop()


@pytest.mark.parametrize("refresh", [False, True])
def test_models_cli_refresh_is_explicit_and_local(monkeypatch, refresh):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def request(self, method, params):
            calls.append((method, params))
            return {"data": []}

        async def stop(self):
            calls.append("stop")

    monkeypatch.setattr("supervisor.runtime.client.RuntimeClient", Client)
    result = CliRunner().invoke(cli, ["runtime", "models", "--engine", "pi", *(["--refresh"] if refresh else [])])
    assert result.exit_code == 0, result.output
    assert calls == [("model/list", {"engines": ["pi"], "optionalEngines": False,
                                     **({"refresh": True} if refresh else {})}), "stop"]
    assert json.loads(result.output) == {"data": []}


def test_models_cli_requires_pi_for_local_refresh():
    result = CliRunner().invoke(cli, ["runtime", "models", "--refresh"])
    assert result.exit_code == 2
    assert "--refresh requires --engine pi" in result.output


def test_pi_sdk_pins_match_the_lock_and_host():
    from supervisor.runtime.install import PINNED_PI_VERSION

    root = Path(__file__).resolve().parents[1] / "supervisor/pi_worker"
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert PINNED_PI_VERSION == "1.0.4"
    for name in ("@earendil-works/pi-ai", "@earendil-works/pi-coding-agent"):
        assert package["dependencies"][name] == PINNED_PI_VERSION
        assert lock["packages"][""]["dependencies"][name] == PINNED_PI_VERSION
    for name, version in package["overrides"].items():
        assert version == PINNED_PI_VERSION
        assert lock["packages"][f"node_modules/{name}"]["version"] == PINNED_PI_VERSION
