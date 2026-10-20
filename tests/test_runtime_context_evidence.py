from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from supervisor.runtime import sandbox as sandbox_module
from supervisor.runtime.sandbox import SandboxPolicy, SandboxRunner, SandboxUnavailableError, _mac_profile
from supervisor.runtime.tools import ToolHost, ToolScope
from supervisor.runtime_context import RuntimeContextStore
from supervisor.schemas import BelloConfig, SupervisorDecision
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError


def test_empty_context_allocates_nothing() -> None:
    context = RuntimeContextStore()
    assert context.root is None
    assert context.paths == {}
    assert context.readable_roots == []
    context.assert_unchanged()
    context.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
def test_export_has_only_exact_readonly_ledger_copies() -> None:
    context = RuntimeContextStore(progress="neutral progress\n", decisions="neutral decisions\n")
    root = context.root
    try:
        assert root is not None
        assert set(path.name for path in root.iterdir()) == {"PROGRESS.md", "DECISIONS.md"}
        assert root not in context.readable_roots
        assert len(context.readable_roots) == 2
        for name, text in (("progress", "neutral progress\n"), ("decisions", "neutral decisions\n")):
            path = Path(context.paths[f"{name}_path"])
            assert path.read_text() == text
            assert path in context.readable_roots
            assert ".supervisor" not in path.parts
            assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert stat.S_IMODE(root.stat().st_mode) == 0o500
        context.assert_unchanged()
    finally:
        context.cleanup()
    assert root is not None and not root.exists()


def test_export_detects_modified_text() -> None:
    context = RuntimeContextStore(progress="neutral original")
    try:
        path = context.readable_roots[0]
        path.chmod(0o600)
        path.write_text("changed")
        with pytest.raises(OSError, match="modified"):
            context.assert_unchanged()
    finally:
        context.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
def test_export_rejects_symlink_replacement_without_reading_target(tmp_path: Path) -> None:
    context = RuntimeContextStore(progress="neutral original")
    target = tmp_path / "neutral-target.txt"
    target.write_text("untouched")
    try:
        assert context.root is not None
        context.root.chmod(0o700)
        path = context.readable_roots[0]
        path.unlink()
        path.symlink_to(target)
        with pytest.raises(OSError, match="removed or replaced"):
            context.assert_unchanged()
    finally:
        context.cleanup()
    assert target.read_text() == "untouched"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
def test_file_authorities_do_not_expose_other_state_or_allow_writes(tmp_path: Path, monkeypatch) -> None:
    # The profile's fixed system roots are host details irrelevant to what is asserted here.
    monkeypatch.setattr(sandbox_module, "_mac_system_roots", lambda: ())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = workspace / ".supervisor"
    private.mkdir()
    (private / "hidden.txt").write_text("neutral hidden fixture")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    context = RuntimeContextStore(progress="neutral history")
    try:
        exported = context.readable_roots[0]
        scope = ToolScope(root=workspace, mode="read-only", readable_roots=tuple(context.readable_roots))
        host = ToolHost.__new__(ToolHost)
        assert host._path(scope, str(exported)) == exported
        with pytest.raises(PermissionError):
            host._path(scope, str(exported.parent / "other.txt"))
        with pytest.raises(PermissionError):
            host._path(scope, str(exported), writing=True)
        policy = SandboxPolicy(workspace, mode="read-only", readable_roots=tuple(context.readable_roots))
        profile, parameters = _mac_profile(policy, scratch)
        assert any(value.endswith(f"={exported}") and value.startswith("BELLO_READ_FILE_") for value in parameters)
        assert not any(value.endswith(f"={exported.parent}") and value.startswith("BELLO_READ_DIR_") for value in parameters)
        assert any(value.endswith(f"={private}") and value.startswith("BELLO_PRIVATE_") for value in parameters)
        assert '(deny file-read* (literal (param "BELLO_PRIVATE_' in profile
    finally:
        context.cleanup()


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS evidence access regression")
async def test_native_sandbox_reads_export_but_denies_private_state_and_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = workspace / ".supervisor"
    private.mkdir()
    hidden = private / "private-neutral.txt"
    hidden.write_text("PRIVATE_NEUTRAL_SENTINEL")
    context = RuntimeContextStore(progress="PUBLIC_NEUTRAL_LEDGER")
    try:
        exported = context.readable_roots[0]
        runner = SandboxRunner(SandboxPolicy(workspace, mode="read-only", readable_roots=tuple(context.readable_roots)))
        try:
            allowed = await runner.run(f"/bin/cat {shlex.quote(str(exported))}", workspace, 10)
        except SandboxUnavailableError as error:
            pytest.skip(f"this host does not allow starting the native macOS sandbox: {error}")
        assert allowed.exit_code == 0, allowed.output
        assert allowed.output == "PUBLIC_NEUTRAL_LEDGER"
        denied = await runner.run(f"/bin/cat {shlex.quote(str(hidden))}", workspace, 10)
        assert denied.exit_code != 0
        assert "PRIVATE_NEUTRAL_SENTINEL" not in denied.output
        write = await runner.run(f"/usr/bin/touch {shlex.quote(str(exported))}", workspace, 10)
        assert write.exit_code != 0
        context.assert_unchanged()
    finally:
        context.cleanup()


class DecisionClient:
    def __init__(self, *, expected: dict[str, str], error: BaseException | None = None) -> None:
        self.expected = expected
        self.error = error
        self.paths: list[Path] = []
        self.thread_params: dict = {}
        self.turn_params: dict = {}
        self.payload: dict = {}
        self.archived = False

    async def thread_start(self, params, *, timeout):
        self.thread_params = params
        assert params["belloRole"] == "runtime"
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"
        return {"thread": {"id": "neutral-runtime"}}

    async def turn_start(self, params, *, timeout):
        self.turn_params = params
        self.payload = json.loads(params["input"][0]["text"])
        for field in ("progress", "decisions"):
            if field not in self.expected:
                assert f"{field}_path" not in self.payload
                continue
            path = Path(self.payload[f"{field}_path"])
            assert path.read_text() == self.expected[field]
            assert str(path) in params["runtimeWorkspaceRoots"]
            assert str(path) in self.thread_params["runtimeWorkspaceRoots"]
            assert str(path.parent) not in params["runtimeWorkspaceRoots"]
            assert ".supervisor" not in path.parts
            self.paths.append(path)
        if self.error is not None:
            raise self.error
        decision = SupervisorDecision(decision="noop", reason="neutral evidence checked")
        return {"turn": {"id": "neutral-turn", "status": "completed", "items": [
            {"type": "agentMessage", "text": decision.model_dump_json()},
        ]}}

    async def thread_archive(self, thread_id, *, timeout):
        assert all(path.exists() for path in self.paths)
        self.archived = True
        return {}


def agent_fixture(tmp_path: Path, client: DecisionClient) -> StatelessSupervisorAgent:
    task = tmp_path / "TASK.md"
    task.write_text("Neutral task: inspect a fixture.")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    return StatelessSupervisorAgent(client, store, task, model="neutral/model")  # type: ignore[arg-type]


@pytest.mark.parametrize("progress,decisions", [("", ""), ("# Progress\n\n- one\n", "# Decisions\n\n- two\n")])
async def test_complete_inline_ledgers_have_no_paths_or_extra_authorities(tmp_path: Path, progress: str, decisions: str) -> None:
    client = DecisionClient(expected={})
    agent = agent_fixture(tmp_path, client)
    packet = agent.build_packet(wake_sequence=3, current_summary="neutral startup")
    packet = packet.model_copy(update={"progress": progress, "decisions": decisions})
    result = await agent.decide(packet)
    assert result.decision == "noop"
    assert result.wake_sequence == 3
    assert client.thread_params["runtimeWorkspaceRoots"] == [str(tmp_path.resolve())]
    assert client.archived
    assert packet.progress_path is None and packet.decisions_path is None


async def test_omitted_ledgers_export_full_text_and_cleanup_after_reviewer(tmp_path: Path) -> None:
    progress = "# Progress\n\n" + "".join(f"- neutral progress {index}\n" for index in range(40))
    decisions = "# Decisions\n\n" + "".join(f"- neutral decision {index}\n" for index in range(30))
    client = DecisionClient(expected={"progress": progress, "decisions": decisions})
    agent = agent_fixture(tmp_path, client)
    packet = agent.build_packet(wake_sequence=7, current_summary="neutral history")
    packet = packet.model_copy(update={"progress": progress, "decisions": decisions})
    result = await agent.decide(packet)
    assert result.decision == "noop"
    assert client.payload["progress_omitted_entries"] == 10
    assert client.payload["decisions_omitted_entries"] == 10
    assert "neutral progress 0\n" not in client.payload["progress"]
    assert client.archived
    assert all(not path.exists() and not path.parent.exists() for path in client.paths)
    assert packet.progress == progress and packet.decisions == decisions
    assert packet.progress_path is None and packet.decisions_path is None


async def test_partial_single_entry_exports_even_when_no_whole_entry_omitted(tmp_path: Path) -> None:
    progress = "# Progress\n\n- " + "n" * 13_000
    client = DecisionClient(expected={"progress": progress})
    agent = agent_fixture(tmp_path, client)
    packet = agent.build_packet(wake_sequence=1, current_summary="neutral long entry")
    packet = packet.model_copy(update={"progress": progress, "decisions": ""})
    await agent.decide(packet)
    assert client.payload.get("progress_omitted_entries", 0) == 0
    assert len(client.payload["progress"]) < len(progress)
    assert all(not path.exists() for path in client.paths)


@pytest.mark.parametrize("failure", [RuntimeError("neutral provider failure"), asyncio.CancelledError()])
async def test_evidence_is_cleaned_on_failure_or_cancellation(tmp_path: Path, failure: BaseException) -> None:
    progress = "# Progress\n\n" + "".join(f"- item {index}\n" for index in range(40))
    client = DecisionClient(expected={"progress": progress}, error=failure)
    agent = agent_fixture(tmp_path, client)
    packet = agent.build_packet(wake_sequence=1, current_summary="neutral failure")
    packet = packet.model_copy(update={"progress": progress, "decisions": ""})
    expected_error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else SupervisorAgentError
    with pytest.raises(expected_error):
        await agent.decide(packet)
    assert client.archived
    assert all(not path.exists() and not path.parent.exists() for path in client.paths)
