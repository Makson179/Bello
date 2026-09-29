"""Keep installation and `bello update` on the same available bundled CLI."""

from pathlib import Path
from types import SimpleNamespace
import tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
import pytest

from supervisor import update_check
from supervisor.runtime.claude import ClaudeBackend


PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def sdk_requirements(extra):
    data = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    return [
        Requirement(raw)
        for raw in data["project"]["optional-dependencies"][extra]
        if Requirement(raw).name == "claude-agent-sdk"
    ]


@pytest.mark.parametrize("platform,version", [
    ("darwin", "0.2.161"), ("linux", "0.2.161"), ("win32", "0.2.159"),
])
@pytest.mark.parametrize("extra", ["claude", "test"])
def test_sdk_extra_has_one_platform_appropriate_pin(platform, version, extra):
    environment = {**default_environment(), "sys_platform": platform, "extra": extra}
    active = [req for req in sdk_requirements(extra)
              if req.marker is None or req.marker.evaluate(environment)]
    assert len(active) == 1
    assert str(active[0].specifier) == f"=={version}"
    assert active[0].url is None


@pytest.mark.parametrize("platform,version", [
    ("darwin", "0.2.161"), ("linux", "0.2.161"), ("win32", "0.2.159"),
])
@pytest.mark.parametrize("already_matching", [False, True])
def test_updater_honors_platform_marker_and_ignores_test_extra(monkeypatch, platform, version, already_matching):
    # Reproduce wheel Requires-Dist markers, including the test extra duplicate.
    requirements = [
        f'{req.name}{req.specifier}; ({req.marker}) and extra == "{extra}"'
        for extra in ("claude", "test") for req in sdk_requirements(extra)
    ]
    environment = {**default_environment(), "sys_platform": platform}
    monkeypatch.setattr("packaging.markers.default_environment", lambda: environment.copy())
    monkeypatch.setattr(update_check.metadata, "distribution", lambda _: SimpleNamespace(requires=requirements))
    installed = [version if already_matching else "0.2.158"]
    monkeypatch.setattr(update_check.metadata, "version", lambda _: installed[0])
    commands = []
    readiness = []
    monkeypatch.setattr(update_check, "_dependency_install_command", lambda req: (["installer", req], None))

    def install(command, **_kwargs):
        commands.append(command)
        installed[0] = version

    monkeypatch.setattr(update_check, "_run_package_command", install)
    monkeypatch.setattr(ClaudeBackend, "_bundled_cli_path", lambda: readiness.append(True) or Path("bundled-cli"))
    update_check._ensure_claude_dependency()
    assert commands == ([] if already_matching else [["installer", f"claude-agent-sdk=={version}"]])
    assert readiness == [True]
