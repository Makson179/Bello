"""Release metadata stays consistent; GitHub-only releases never trigger PyPI."""
import json
import os
from pathlib import Path
import subprocess
import textwrap
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _publication_guard():
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    return textwrap.dedent(workflow.split("          python - <<'PY'\n", 1)[1].split("          PY\n", 1)[0])


def test_release_versions_are_consistent():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version == "0.7.2"
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
    advisor = ROOT / "plugins/bello/skills/bello-config-advisor"
    assert f'TARGET_VERSION = "{version}"' in (advisor / "scripts/inspect_config.py").read_text()


def test_github_only_marker_gates_every_publish_dependency():
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "github.event_name == 'release' && startsWith(github.event.release.tag_name, 'v') && !contains(github.event.release.body, '<!-- bello:github-only -->')" in workflow
    assert "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in workflow
    assert "ref: ${{ needs.build.outputs.source_sha }}" in workflow
    windows = workflow.split("  build-windows:\n", 1)[1].split("  publish-testpypi:", 1)[0]
    assert "needs: build" in windows
    for job in ("publish-testpypi", "publish-pypi"):
        block = workflow.split(f"  {job}:\n", 1)[1].split("\n  publish-", 1)[0]
        assert "needs: [build, build-windows]" in block
        assert "always()" not in block


@pytest.mark.parametrize("case", [
    "manual", "automatic", "wrong_tag", "wrong_branch", "draft",
    "prerelease_mismatch", "github_only", "unrelated_tag", "missing_release",
])
def test_release_publication_guards(case, monkeypatch, tmp_path):
    guard = _publication_guard()
    monkeypatch.chdir(ROOT)
    output = tmp_path / "outputs"
    for key, value in {
        "RELEASE_TAG": "v0.7.2" if case != "wrong_tag" else "v0.7.1",
        "GITHUB_EVENT_NAME": "release" if case == "automatic" else "workflow_dispatch",
        "GITHUB_REF": "refs/heads/other" if case == "wrong_branch" else "refs/heads/main",
        "GITHUB_REPOSITORY": "Makson179/Bello",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    def check_output(args, *, text):
        assert text
        calls.append(args)
        if args[0] == "gh":
            if case == "missing_release":
                raise subprocess.CalledProcessError(1, args)
            return json.dumps({
                "isDraft": case == "draft",
                "isPrerelease": case == "prerelease_mismatch",
                "body": "<!-- bello:github-only -->" if case == "github_only" else "Release notes",
            })
        assert args in (
            ["git", "rev-parse", "--verify", "refs/tags/v0.7.2^{commit}"],
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        )
        return "a" * 40 + "\n"

    def run(args, *, check):
        assert check
        assert args == ["git", "merge-base", "--is-ancestor", "a" * 40, "a" * 40]
        calls.append(args)
        if case == "unrelated_tag":
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    if case in {"manual", "automatic"}:
        exec(compile(guard, "publish.yml:version", "exec"), {})
        assert output.read_text() == f"prerelease=false\nsource_sha={'a' * 40}\n"
        assert any(args[0] == "gh" for args in calls) == (case == "manual")
        assert ["git", "merge-base", "--is-ancestor", "a" * 40, "a" * 40] in calls
    else:
        with pytest.raises((SystemExit, subprocess.CalledProcessError)):
            exec(compile(guard, "publish.yml:version", "exec"), {})
        assert not output.exists()
        if case in {"wrong_tag", "wrong_branch"}:
            assert not calls


@pytest.mark.parametrize("event", ["release", "workflow_dispatch"])
@pytest.mark.parametrize("topology", [
    "exact_main", "main_advanced", "later_checkout", "off_main", "missing_main", "missing_tag",
])
def test_publication_requires_exact_tagged_checkout_on_main(event, topology, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    def git(*args):
        return subprocess.check_output([
            "git", "-c", "user.name=Release test", "-c", "user.email=release@example.invalid",
            *args,
        ], text=True, stderr=subprocess.PIPE).strip()

    git("init", "--initial-branch=main")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.7.2"\n')
    git("add", "pyproject.toml")
    git("commit", "-m", "release base")
    main = git("rev-parse", "HEAD")
    if topology == "off_main":
        git("checkout", "-b", "unmerged-release")
        git("commit", "--allow-empty", "-m", "unmerged release")
    tagged = git("rev-parse", "HEAD")
    if topology != "missing_tag":
        git("tag", "-a", "v0.7.2", "-m", "release tag")
    if topology in {"main_advanced", "later_checkout"}:
        git("commit", "--allow-empty", "-m", "later main")
        main = git("rev-parse", "HEAD")
        if topology == "main_advanced":
            git("checkout", "--detach", tagged)
    if topology != "missing_main":
        git("update-ref", "refs/remotes/origin/main", main)

    output = tmp_path / "outputs"
    for key, value in {
        "RELEASE_TAG": "v0.7.2",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": "refs/tags/v0.7.2" if event == "release" else "refs/heads/main",
        "GITHUB_REPOSITORY": "example/release-test",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    real_check_output = subprocess.check_output

    def check_output(args, **kwargs):
        if args[0] == "gh":
            return json.dumps({"isDraft": False, "isPrerelease": False, "body": "Release notes"})
        assert args[0] == "git"
        return real_check_output(args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", check_output)
    if topology in {"exact_main", "main_advanced"}:
        exec(compile(_publication_guard(), "publish.yml:version", "exec"), {})
        assert output.read_text() == f"prerelease=false\nsource_sha={tagged}\n"
    else:
        with pytest.raises((SystemExit, subprocess.CalledProcessError)):
            exec(compile(_publication_guard(), "publish.yml:version", "exec"), {})
        assert not output.exists()
