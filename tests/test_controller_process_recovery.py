"""Controller restoration and refusals, including two actual Python workers."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from supervisor.controller import BelloController
from supervisor.controller_recovery import (
    DurableRun, RecoveryBlocked, RunOwner, _reject_uncertain_tools,
    recovery_disposition, recovery_root, resume_coder,
)
from supervisor.runtime.journal import RuntimeJournal
from supervisor.schemas import AppEventSource, BelloStatus
from supervisor.state import CONFIG, DECISIONS
from tests.support.controller import _FakeTUI


def _controller(project: Path, **kwargs) -> BelloController:
    return BelloController(project, task_path=project / "TASK.md", client=SimpleNamespace(),
        tui=_FakeTUI(), runtime_enabled=False, completion_review=False, adversary_enabled=False,
        use_git_diff=False, **kwargs)


@pytest.fixture
def saved_run(tmp_path, request, monkeypatch):
    if getattr(request, "param", None) == "custom_prompt":
        from supervisor.prompts import supervisor as prompt_module
        prompt = tmp_path / "custom-prompts.toml"
        shutil.copyfile(Path(prompt_module.__file__).with_name("prompts.toml"), prompt)
        monkeypatch.setenv("BELLO_PROMPTS_FILE", str(prompt))
    (tmp_path / "TASK.md").write_text("Implement the task.\n")
    (tmp_path / "solution.py").write_text("VALUE = 0\n")
    c = _controller(tmp_path)
    with RunOwner(tmp_path, controller=True):
        c._durable_run = DurableRun(c)
        c.initialize_state()
        c._prepare_coder_workspace()
        c.store.update_bello_config(lambda cfg: cfg.model_copy(update={
            "status": BelloStatus.RUNNING, "coder_thread_id": "persisted-coder-thread"}))
        c._append_event(AppEventSource.SYSTEM, "fixture/observed")
        c._write_run_checkpoint("coder", state="active")
        assert c._durable_run.record.eligible
    yield c
    snapshot = c._coder_snapshot
    if snapshot is not None and snapshot.temp_root.exists():
        snapshot.cleanup()


def _bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def _restore_attempt(c):
    restored = _controller(c.project_root)
    with RunOwner(c.project_root, controller=True):
        restored._durable_run = DurableRun(restored)
        restored.initialize_state()
    return restored


@pytest.mark.parametrize("corruption", ["config", "task", "state", "duplicate", "negative", "terminal", "schema", "scalar"])
def test_invalid_recovery_never_starts_or_modifies_preserved_state(saved_run, corruption):
    c = saved_run
    path = recovery_root(c.project_root) / "run.json"
    if corruption == "config":
        data = json.loads(c.store.path(CONFIG).read_text())
        data["max_restarts"] += 1
        c.store.path(CONFIG).write_text(json.dumps(data))
    elif corruption == "task":
        c.task_path.write_text("A different task.\n")
    elif corruption == "state":
        c.store.path(DECISIONS).write_text("Unrecorded decision.\n")
    elif corruption == "duplicate":
        path.write_text(path.read_text().replace('"version": 2', '"version": 2, "version": 2'))
    else:
        data = json.loads(path.read_text())
        if corruption == "negative":
            data["config"]["restart_count"] = -1
        elif corruption == "terminal":
            data["terminal"] = True
        elif corruption == "scalar":
            data["controller"]["scalars"]["_runtime_apply_retry_count"] = -1
        else:
            data["version"] = 99
        path.write_text(json.dumps(data))
    before = _bytes(c.store.state_dir)
    with pytest.raises(RecoveryBlocked):
        _restore_attempt(c)
    assert _bytes(c.store.state_dir) == before


def test_orderly_shutdown_claim_still_requires_verified_tree_fence(saved_run):
    c = saved_run
    path = recovery_root(c.project_root) / "run.json"
    data = json.loads(path.read_text())
    data["clean_shutdown"] = True
    path.write_text(json.dumps(data))
    before = _bytes(c.store.state_dir)
    with pytest.raises(RuntimeError, match="fenc"):
        _restore_attempt(c)
    assert _bytes(c.store.state_dir) == before


def test_changed_implementation_blocks_before_any_state_write(saved_run, monkeypatch):
    import supervisor.controller_recovery as recovery
    before = _bytes(saved_run.store.state_dir)
    monkeypatch.setattr(recovery, "implementation_fingerprint", lambda: "0" * 64)
    with pytest.raises(RecoveryBlocked, match="code or dependency environment changed"):
        _restore_attempt(saved_run)
    assert _bytes(saved_run.store.state_dir) == before


def test_prompt_toml_changes_implementation_fingerprint_in_copied_package(tmp_path, monkeypatch):
    import supervisor.controller_recovery as recovery
    package = tmp_path / "supervisor"
    shutil.copytree(Path(recovery.__file__).parent, package,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    prompt = package / "prompts" / "prompts.toml"
    assert prompt.is_file()
    with monkeypatch.context() as scoped:
        scoped.setattr(recovery, "__file__", str(package / "controller_recovery.py"))
        recovery.implementation_fingerprint.cache_clear()
        try:
            before = recovery.implementation_fingerprint()
            prompt.write_text(prompt.read_text(encoding="utf-8") + "\n# Changed installed prompt asset.\n", encoding="utf-8")
            # A running owner retains its original identity; a new process
            # computes a fresh identity and refuses the changed prompt bundle.
            assert recovery.implementation_fingerprint() == before
            recovery.implementation_fingerprint.cache_clear()
            assert recovery.implementation_fingerprint() != before
        finally:
            recovery.implementation_fingerprint.cache_clear()


def test_duplicate_distribution_discovery_does_not_change_identity_but_real_dependency_change_does(tmp_path, monkeypatch):
    import supervisor.controller_recovery as recovery
    def distribution(name, version, root):
        return SimpleNamespace(metadata={"Name": name}, version=version, locate_file=lambda _: root)
    root = tmp_path / "installed"
    entries = [distribution("Fixture-Package", "1.0", root)]
    with monkeypatch.context() as scoped:
        scoped.setattr(recovery, "distributions", lambda: iter(entries))
        recovery.implementation_fingerprint.cache_clear()
        try:
            before = recovery.implementation_fingerprint()
            entries.extend([distribution("fixture_package", "1.0", root)] * 9)
            recovery.implementation_fingerprint.cache_clear()
            assert recovery.implementation_fingerprint() == before
            entries[:] = [distribution("Fixture-Package", "2.0", root)]
            recovery.implementation_fingerprint.cache_clear()
            assert recovery.implementation_fingerprint() != before
            entries[:] = [distribution("Fixture-Package", "1.0", tmp_path / "other-install")]
            recovery.implementation_fingerprint.cache_clear()
            assert recovery.implementation_fingerprint() != before
        finally:
            recovery.implementation_fingerprint.cache_clear()


def test_changed_engine_binary_blocks_before_any_state_write(saved_run, tmp_path):
    c = saved_run
    # Python 3.14's Windows lookup requires a PATHEXT executable suffix even
    # for an absolute path. This identity-only fixture is never launched.
    binary = tmp_path / "engine-binary.exe"
    binary.write_bytes(b"first engine")
    c.client.command = [str(binary)]
    binary.chmod(0o700)
    c._write_run_checkpoint("coder")
    binary.write_bytes(b"different engine")
    before = _bytes(c.store.state_dir)
    with pytest.raises(RecoveryBlocked, match="execution-engine files changed"):
        _restore_attempt(c)
    assert _bytes(c.store.state_dir) == before


def test_changed_engine_selection_rejected_before_continuation(saved_run):
    owner = saved_run._durable_run
    owner.expected_engines = {"client": {"backend": "some.other.Backend"}}
    with pytest.raises(RecoveryBlocked, match="selected execution engine changed"):
        owner.verify_engine_selection()


@pytest.mark.parametrize("saved_run", ["custom_prompt"], indirect=True)
@pytest.mark.parametrize("change", ["path", "contents"])
def test_changed_effective_prompt_blocks_before_state_write(saved_run, monkeypatch, change):
    prompt = Path(os.environ["BELLO_PROMPTS_FILE"])
    if change == "path":
        other = prompt.with_name("other-prompts.toml")
        shutil.copyfile(prompt, other)
        monkeypatch.setenv("BELLO_PROMPTS_FILE", str(other))
    else:
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\n# changed override\n", encoding="utf-8")
    before = _bytes(saved_run.store.state_dir)
    with pytest.raises(RecoveryBlocked, match="effective prompt source or contents changed"):
        _restore_attempt(saved_run)
    assert _bytes(saved_run.store.state_dir) == before


@pytest.mark.parametrize("saved_run", ["custom_prompt"], indirect=True)
def test_live_prompt_override_change_marks_run_nonresumable_without_blessing_new_bytes(saved_run):
    original = saved_run._durable_run.record.prompt_identity
    prompt = Path(os.environ["BELLO_PROMPTS_FILE"])
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n# live edit\n", encoding="utf-8")
    saved_run._write_run_checkpoint("coder")
    assert not saved_run._durable_run.record.eligible
    assert saved_run._durable_run.record.prompt_identity == original
    assert "prompt source changed" in saved_run._durable_run.record.reason


def test_full_access_never_trusts_recovery_authority(saved_run, monkeypatch):
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "danger-full-access")
    saved_run._write_run_checkpoint("coder")
    assert not recovery_disposition(saved_run.project_root)["eligible"]
    # Even a forged eligibility bit must not make writable authority trusted.
    path = recovery_root(saved_run.project_root) / "run.json"
    record = json.loads(path.read_text())
    record["eligible"] = True
    path.write_text(json.dumps(record))
    before = _bytes(saved_run.store.state_dir)
    with pytest.raises(RecoveryBlocked, match="full-access coder"):
        _restore_attempt(saved_run)
    assert _bytes(saved_run.store.state_dir) == before


def test_changed_native_selector_never_starts_new_executable_or_mutates_state(saved_run, tmp_path, tmp_path_factory, monkeypatch):
    from supervisor.runtime.client import RuntimeClient
    from supervisor.runtime.codex import CodexBackend
    c = saved_run
    old = tmp_path_factory.mktemp("native-selector") / ("old-native.exe" if os.name == "nt" else "old-native")
    old.write_bytes(b"MZfixture-native-identity" if os.name == "nt" else b"\x7fELFfixture-native-identity")
    old.chmod(0o700)
    monkeypatch.setenv("BELLO_CODEX_BINARY", str(old))
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    client = RuntimeClient(cwd=c.project_root)
    client._journal = RuntimeJournal(client.state_dir)
    backend = CodexBackend(state_dir=client.state_dir / "codex", emit=lambda _: None)
    backend._native_command = [str(old), "app-server", "--listen", "stdio://"]
    client._engines["codex"] = backend
    c.client = client
    c._write_run_checkpoint("coder")
    assert c._durable_run.record.eligible
    from supervisor.controller_recovery import _verify_planned_engine_selection
    # The unchanged native selector is accepted without constructing or
    # starting a backend in the fresh client.
    _verify_planned_engine_selection(SimpleNamespace(client=RuntimeClient(cwd=c.project_root)),
                                    c.store.get_bello_config(), c._durable_run.record.engine_identity)
    backend._journal.close()
    client._journal.close()
    marker = tmp_path / "new-executable-started"
    # Python is the real replacement executable. Its app-server argument is a
    # controlled script in the native cwd; if launched it writes this sentinel.
    script = client.state_dir / "codex" / "app-server"
    script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('started')\n", encoding="utf-8")
    probe = subprocess.run([sys.executable, "app-server", "--listen", "stdio://"],
                           cwd=script.parent, capture_output=True, timeout=20)
    assert probe.returncode == 0 and marker.read_text() == "started"
    marker.unlink()
    before = _bytes(c.store.state_dir)
    code = """
import asyncio, sys
from pathlib import Path
from supervisor.controller import BelloController
from supervisor.runtime.client import RuntimeClient
from tests.support.controller import _FakeTUI
p = Path(sys.argv[1])
c = BelloController(p, task_path=p / 'TASK.md', client=RuntimeClient(cwd=p),
    tui=_FakeTUI(), runtime_enabled=False, completion_review=False,
    adversary_enabled=False, use_git_diff=False)
asyncio.run(c.run())
"""
    environment = {**os.environ, "BELLO_CODEX_BINARY": sys.executable}
    result = subprocess.run([sys.executable, "-B", "-c", code, str(c.project_root)],
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "selected execution engine changed before recovery" in result.stderr
    assert not marker.exists()
    assert _bytes(c.store.state_dir) == before


@pytest.mark.parametrize("engine", ["claude-code", "pi"])
def test_expected_claude_and_pi_selectors_are_resolved_without_process_probes(saved_run, monkeypatch, engine):
    from supervisor.controller_recovery import _verify_planned_engine_selection
    from supervisor.runtime.client import RuntimeClient
    executable = Path(sys.executable).resolve()
    if engine == "claude-code":
        from supervisor.runtime.claude import ClaudeBackend
        def official(*, prepare=False):
            assert prepare is False
            return SimpleNamespace(path=executable)
        monkeypatch.setattr(ClaudeBackend, "_official_cli", official)
        command = [str(executable)]
        backend = "supervisor.runtime.claude.ClaudeBackend"
    else:
        import supervisor.runtime.install as install
        monkeypatch.setenv("BELLO_NODE", str(executable))
        monkeypatch.setattr(install, "worker_directory", lambda: saved_run.project_root)
        monkeypatch.setattr(install, "worker_command", lambda: pytest.fail("worker_command executes Node --version"))
        command = [str(executable), str(saved_run.project_root / "worker.mjs")]
        backend = "supervisor.runtime.transport.WorkerTransport"
    expected = {engine: {"backend": backend, "command": json.dumps(command, separators=(",", ":")),
                         "executable": str(executable)}}
    fresh = SimpleNamespace(client=RuntimeClient(cwd=saved_run.project_root))
    _verify_planned_engine_selection(fresh, saved_run.store.get_bello_config(), expected)


def test_native_path_redirection_is_rejected_read_only(saved_run, tmp_path_factory, monkeypatch):
    from supervisor.controller_recovery import _verify_planned_engine_selection
    from supervisor.runtime.client import RuntimeClient
    native_root = tmp_path_factory.mktemp("native-path-selector")
    old, changed = native_root / "old-bin", native_root / "new-bin"
    old.mkdir()
    changed.mkdir()
    name = "codex.exe" if os.name == "nt" else "codex"
    for directory in (old, changed):
        path = directory / name
        path.write_bytes(b"MZfixture" if os.name == "nt" else b"\x7fELFfixture")
        path.chmod(0o700)
    command = [name, "app-server", "--listen", "stdio://"]
    expected = {"codex": {"backend": "supervisor.runtime.codex.CodexBackend",
        "command": json.dumps(command, separators=(",", ":")), "executable": str((old / name).resolve())}}
    monkeypatch.setenv("BELLO_CODEX_BINARY", name)
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    monkeypatch.setenv("PATH", str(changed))
    fresh = SimpleNamespace(client=RuntimeClient(cwd=saved_run.project_root))
    before = _bytes(saved_run.store.state_dir)
    with pytest.raises(RecoveryBlocked, match="selected execution engine changed"):
        _verify_planned_engine_selection(fresh, saved_run.store.get_bello_config(), expected)
    assert _bytes(saved_run.store.state_dir) == before


@pytest.mark.parametrize("phase", ["runtime_review", "completion_review", "adversary", "finalizing"])
def test_ambiguous_phase_is_not_automatically_replayed(saved_run, phase):
    c = saved_run
    c._write_run_checkpoint(phase, state="active")
    assert not recovery_disposition(c.project_root)["eligible"]
    before = _bytes(c.store.state_dir)
    with pytest.raises(RecoveryBlocked, match="manual recovery"):
        _restore_attempt(c)
    assert _bytes(c.store.state_dir) == before


def test_paused_controller_cannot_restart_work_automatically(saved_run):
    c = saved_run
    c.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.PAUSED}))
    c._write_run_checkpoint("paused")
    assert not recovery_disposition(c.project_root)["eligible"]


def test_recovery_disabled_cannot_reset_an_existing_unfinished_run(saved_run):
    c = saved_run
    before = _bytes(c.store.state_dir)
    disabled = _controller(c.project_root, recovery_enabled=False)
    with RunOwner(c.project_root, controller=True):
        disabled._durable_run = DurableRun(disabled)
        with pytest.raises(RecoveryBlocked, match="unfinished run requires recovery"):
            disabled.initialize_state()
    assert _bytes(c.store.state_dir) == before


def test_legacy_running_state_without_a_thread_is_not_silently_reset(tmp_path):
    (tmp_path / "TASK.md").write_text("task")
    c = _controller(tmp_path)
    c.initialize_state()
    c.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.RUNNING}))
    with RunOwner(tmp_path, controller=True):
        c._durable_run = DurableRun(c)
    # Read the complete state only outside the mandatory Windows byte lock.
    # Creating the lock first keeps it included in the exact before/after check.
    before = _bytes(c.store.state_dir)
    with RunOwner(tmp_path, controller=True):
        with pytest.raises(RecoveryBlocked, match="legacy interrupted run"):
            c.initialize_state()
    assert _bytes(c.store.state_dir) == before


def test_uncertain_or_later_completed_tools_block_restore(tmp_path):
    directory = tmp_path / "runtime"
    journal = RuntimeJournal(directory)
    digest, uncertain = journal.recovery_tool_state()
    assert not uncertain
    journal.claim_tool("thread", "call", "exec_command", {"command": "side effect"})
    with pytest.raises(RecoveryBlocked, match="uncertain"):
        _reject_uncertain_tools(str(directory), expected_digest=digest)
    journal.complete_tool("thread", "call", {"result": "completed after checkpoint"})
    with pytest.raises(RecoveryBlocked, match="changed after"):
        _reject_uncertain_tools(str(directory), expected_digest=digest)
    current, uncertain = journal.recovery_tool_state()
    assert current != digest and not uncertain
    journal.close()
    _reject_uncertain_tools(str(directory), expected_digest=current)


async def test_provider_completed_unobserved_action_does_not_start_new_turn(saved_run):
    c = saved_run
    c.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "old"}))
    starts = []
    class Coder:
        async def resume_thread(self):
            return {"turns": [{"id": "old", "status": "interrupted", "items": [
                {"id": "command", "type": "commandExecution", "status": "completed", "exitCode": 0}]}]}
        async def start_turn(self, text):
            starts.append(text)
    c.coder = Coder()
    with pytest.raises(RecoveryBlocked, match="not accounted"):
        await resume_coder(c)
    assert starts == []


async def test_concurrent_controllers_in_same_python_process_are_exclusive(tmp_path):
    (tmp_path / "TASK.md").write_text("task")
    (tmp_path / "preserve.txt").write_text("must survive")
    c1, c2 = _controller(tmp_path), _controller(tmp_path, clean_workspace=True)
    acquired, release = asyncio.Event(), asyncio.Event()
    async def held():
        acquired.set()
        await release.wait()
    c1._run_owned = held
    first = asyncio.create_task(c1.run())
    await acquired.wait()
    try:
        with pytest.raises(RecoveryBlocked, match="owns"):
            await c2.run()
        assert (tmp_path / "preserve.txt").read_text() == "must survive"
    finally:
        release.set()
        await first


def test_cli_to_controller_nesting_does_not_allow_a_second_controller(tmp_path):
    with RunOwner(tmp_path):
        with RunOwner(tmp_path, controller=True):
            with pytest.raises(RecoveryBlocked, match="another Bello controller"):
                with RunOwner(tmp_path, controller=True):
                    pytest.fail("two controllers acquired one workspace")


def test_another_python_process_cannot_take_controller_ownership(tmp_path):
    code = "from pathlib import Path; from supervisor.controller_recovery import RunOwner; import sys; RunOwner(Path(sys.argv[1])).__enter__()"
    with RunOwner(tmp_path, controller=True):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "another Bello controller owns" in result.stderr


def test_actual_controller_process_crash_and_relaunch_with_synthetic_fence(tmp_path, monkeypatch):
    """Synthetic receipt isolates controller correctness from native guardian QA."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "TASK.md").write_text("Set VALUE to 43.\n")
    (project / "solution.py").write_text("VALUE = 0\n")
    evidence = tmp_path / "provider-events.jsonl"
    command = [sys.executable, "-B", "-m", "tests.support.controller_recovery_process", str(project), str(evidence)]
    environment = dict(os.environ)
    for name in ("BELLO_WATCHDOG_WORKER", "BELLO_WATCHDOG_FENCE_TOKEN", "BELLO_RECOVERY_PERMIT", "BELLO_FENCE_ADDRESS", "BELLO_FENCE_SECRET"):
        environment.pop(name, None)
    first = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=40)
    assert first.returncode == 77, first.stderr
    old = json.loads((recovery_root(project) / "run.json").read_text())
    assert old["eligible"] and not old["terminal"]
    assert (project / "solution.py").read_text() == "VALUE = 0\n"
    nonce = secrets.token_hex(32)
    receipt = {"version": 1, "scope": "tree", "run_id": old["run_id"], "owner_pid": old["owner_pid"],
               "owner_epoch": old["owner_epoch"], "nonce": nonce}
    (recovery_root(project) / "fence-receipt.json").write_text(json.dumps(receipt))
    environment["BELLO_RECOVERY_PERMIT"] = nonce
    second = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=40)
    assert second.returncode == 0, second.stderr
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert sum(row["kind"] == "initial_thread" for row in rows) == 1
    assert sum(row["kind"] == "initial_turn" for row in rows) == 1
    assert sum(row["kind"] == "continuation_turn" for row in rows) == 1
    before = next(row for row in rows if row["kind"] == "crash_checkpoint")
    after = next(row for row in rows if row["kind"] == "restored_evidence")
    assert before["pid"] != after["pid"]
    for key in ("run_id", "snapshot", "generation", "restarts", "sequence"):
        assert before[key] == after[key]
    assert (project / "solution.py").read_text() == "VALUE = 43\n"
    assert not Path(before["snapshot"]).exists()
    final = json.loads((recovery_root(project) / "run.json").read_text())
    assert final["run_id"] == old["run_id"] and final["terminal"] and not final["eligible"]
    log = [json.loads(line) for line in (project / ".supervisor" / "log.jsonl").read_text().splitlines()]
    assert sum(row.get("type") == "coder_snapshot_patch_applied" for row in log) == 1
