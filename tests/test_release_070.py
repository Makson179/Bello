"""Release metadata stays consistent; GitHub-only releases never trigger PyPI."""
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_consistent():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version == "0.7.0"
    for name in ("plugins/bello/.codex-plugin/plugin.json",
                 "plugins/bello/.claude-plugin/plugin.json",
                 "supervisor/pi_worker/package.json",
                 "supervisor/pi_worker/package-lock.json"):
        assert json.loads((ROOT / name).read_text())["version"] == version
    lock = json.loads((ROOT / "supervisor/pi_worker/package-lock.json").read_text())
    assert lock["packages"][""]["version"] == version
    claude = (ROOT / "supervisor/runtime/claude.py").read_text()
    assert claude.count(f'"CLAUDE_AGENT_SDK_CLIENT_APP": "bello/{version}"') == 2
    assert f'version="{version}"' in claude
    assert f'version: "{version}"' in (ROOT / "supervisor/pi_worker/src/runtime.mjs").read_text()


def test_github_only_marker_gates_every_publish_dependency():
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "if: startsWith(github.event.release.tag_name, 'v') && !contains(github.event.release.body, '<!-- bello:github-only -->')" in workflow
    windows = workflow.split("  build-windows:\n", 1)[1].split("  publish-testpypi:", 1)[0]
    assert "needs: build" in windows
    for job in ("publish-testpypi", "publish-pypi"):
        block = workflow.split(f"  {job}:\n", 1)[1].split("\n  publish-", 1)[0]
        assert "needs: [build, build-windows]" in block
        assert "always()" not in block
