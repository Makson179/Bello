"""Keep installation and `bello update` on the same official Claude Code CLI.

Bello pins one claude-agent-sdk release on every platform. The readiness
contract in ``supervisor.runtime.claude_cli`` selects the explicit managed
CLI pair even when that SDK's wheel contains a bundled CLI.
"""

from pathlib import Path
from types import SimpleNamespace
import tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
import pytest

from supervisor import update_check
from supervisor.runtime import claude_cli
from supervisor.runtime.claude import ClaudeBackend


PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
PINNED_SDK = "0.2.164"


def sdk_requirements(extra):
    data = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    return [
        Requirement(raw)
        for raw in data["project"]["optional-dependencies"][extra]
        if Requirement(raw).name == "claude-agent-sdk"
    ]


@pytest.mark.parametrize("platform,version", [
    ("darwin", PINNED_SDK), ("linux", PINNED_SDK), ("win32", PINNED_SDK),
])
@pytest.mark.parametrize("extra", ["claude", "test"])
def test_sdk_extra_has_one_platform_appropriate_pin(platform, version, extra):
    environment = {**default_environment(), "sys_platform": platform, "extra": extra}
    active = [req for req in sdk_requirements(extra)
              if req.marker is None or req.marker.evaluate(environment)]
    assert len(active) == 1
    assert str(active[0].specifier) == f"=={version}"
    assert active[0].url is None


def _wheel_marker(req, extra):
    # Reproduce wheel Requires-Dist markers, including the test extra duplicate.
    extra_marker = f'extra == "{extra}"'
    return f"({req.marker}) and {extra_marker}" if req.marker is not None else extra_marker


@pytest.mark.parametrize("platform,version", [
    ("darwin", PINNED_SDK), ("linux", PINNED_SDK), ("win32", PINNED_SDK),
])
@pytest.mark.parametrize("already_matching", [False, True])
def test_updater_honors_platform_marker_and_ignores_test_extra(monkeypatch, platform, version, already_matching):
    requirements = [
        f"{req.name}{req.specifier}; {_wheel_marker(req, extra)}"
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

    def official_cli(*, prepare=False):
        readiness.append(prepare)
        return claude_cli.OfficialCli(Path("official-cli"), "sdk-bundle")

    monkeypatch.setattr(ClaudeBackend, "_official_cli", staticmethod(official_cli))
    update_check._ensure_claude_dependency()
    assert commands == ([] if already_matching else [["installer", f"claude-agent-sdk=={version}"]])
    # The update prepares, not merely inspects, the explicitly paired official
    # managed CLI through the shared contract on every supported platform.
    assert readiness == [True]


@pytest.mark.parametrize("platform", [
    ("Darwin", "arm64"), ("Darwin", "x86_64"),
    ("Linux", "arm64"), ("Linux", "x86_64"), ("Windows", "x86_64"),
])
def test_managed_cli_is_paired_with_the_pinned_sdk_release(platform):
    release = claude_cli.MANAGED_RELEASES[platform]
    for extra in ("claude", "test"):
        (requirement,) = sdk_requirements(extra)
        assert str(requirement.specifier) == f"=={release.sdk_version}"
    assert release.cli_version == "2.1.293"
    assert release.sdk_bundled_cli_version == "2.1.292"
    assert release.url == (f"https://downloads.claude.ai/claude-code-releases/2.1.293/"
                           f"{release.platform}/{release.binary}")
    assert len(release.sha256) == 64 and int(release.sha256, 16) >= 0
    assert set(claude_cli.MANAGED_RELEASES) == {
        ("Darwin", "arm64"), ("Darwin", "x86_64"),
        ("Linux", "arm64"), ("Linux", "x86_64"), ("Windows", "x86_64"),
    }


def test_update_readiness_failure_is_an_actionable_update_error(monkeypatch):
    monkeypatch.setattr(update_check.metadata, "distribution", lambda _: SimpleNamespace(
        requires=[f'claude-agent-sdk=={PINNED_SDK}; extra == "claude"']))
    monkeypatch.setattr(update_check.metadata, "version", lambda _: PINNED_SDK)

    def not_ready(*, prepare=False):
        raise claude_cli.ClaudeCliError("could not reach downloads.claude.ai", kind="download")

    monkeypatch.setattr(ClaudeBackend, "_official_cli", staticmethod(not_ready))
    with pytest.raises(update_check.UpdateCheckError, match="Claude Code support is not ready: could not reach"):
        update_check._ensure_claude_dependency()
