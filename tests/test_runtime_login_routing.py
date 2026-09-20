from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from supervisor.main import cli


@pytest.mark.parametrize("provider", ["openai-codex", "claude-code", "openai"])
def test_login_keeps_native_and_api_auth_routes_separate(monkeypatch, tmp_path, provider):
    calls = []
    monkeypatch.setenv("BELLO_CODEX_BINARY", "/explicit/codex")
    monkeypatch.setattr("subprocess.run", lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("supervisor.runtime.claude.ClaudeBackend._bundled_cli_path", lambda: tmp_path / "claude")
    worker = tmp_path / "worker.mjs"
    worker.with_name("auth.mjs").write_text("fixture")
    monkeypatch.setattr("supervisor.runtime.install.worker_command", lambda: ["/trusted/node", str(worker)])
    result = CliRunner().invoke(cli, ["runtime", "login", provider])
    assert result.exit_code == 0, result.output
    expected = {
        "openai-codex": ["/explicit/codex", "login"],
        "claude-code": [str(tmp_path / "claude"), "auth", "login"],
        "openai": ["/trusted/node", str(worker.with_name("auth.mjs")), "openai"],
    }
    assert calls == [expected[provider]]
