from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervisor.controller as controller_module
import supervisor.policy as policy_module
import supervisor.workspace_snapshot as workspace_snapshot_module
from supervisor.approvals import ApprovalManager
from supervisor.controller import (
    ADVERSARY_MODEL,
    NO_MARKER_IDLE_NUDGE,
    POST_RESTART_CONTINUE_NUDGE,
    ControllerEvent,
    BelloController,
    _selected_model_availability,
    _canonical_restart_command,
    _ensure_internal_runtime_git_excluded,
    _has_malformed_readiness_marker,
    _has_passing_behavioral_validation,
    _has_readiness_marker,
    _git_status_entries_from_porcelain_v1_z,
    _inspection_from_action,
    _hash_file,
    _path_from_git_status_line,
    _read_workspace_file,
    _runtime_restart_issue,
    _sandbox_matches_mode,
    _evidence_provenance_summary,
    _file_kind,
    _validation_from_action,
    _validation_freshness_summary,
)
from supervisor.adversary_agent import AdversaryAgentError
from supervisor.approvals import normalize_approval_request
from supervisor.appserver import APP_SERVER_CODER_RPC_TIMEOUT_SECONDS, AppServerError, AppServerMessage, AppServerTimeoutError
from supervisor.coder import (
    CODEX_FAST_SERVICE_TIER,
    CoderSession,
    build_multi_agent_developer_instructions,
    coder_thread_params,
    coder_thread_resume_params,
    coder_turn_params,
)
from supervisor.main import _run_async_cleanly
from supervisor.project_config import (
    DEFAULT_MODEL,
    MODEL_GPT_5_5,
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_SOL,
    MODEL_GPT_5_6_TERRA,
    MultiAgentConfig,
    ProjectConfig,
    SubagentDefaultConfig,
)
from supervisor.schemas import (
    AdvReportControllerDecision,
    AppEvent,
    AppEventSource,
    AdversaryReport,
    ApprovalDecisionKind,
    ChangedFile,
    ChangedFileDiff,
    CheapRuntimeDecision,
    CoderMessage,
    CompletionReviewDecision,
    FinalReport,
    HumanMessage,
    InspectionRun,
    PriorIntervention,
    RestartHandoff,
    BelloConfig,
    BelloStatus,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorWakePacket,
    TriggeringAction,
    ValidationRun,
)
from supervisor.state import (
    CONFIG,
    DECISIONS,
    EVENTS,
    FINAL_REPORT,
    HANDOFF,
    LOG,
    PREVIOUS_RUNS,
    PROGRESS,
    RECOVERY,
    RUN_CHECKPOINT,
    RUNTIME_METRICS,
    RUNTIME_TRACE,
    SUPERVISOR_WAKES,
    StateStore,
)
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError
from supervisor.workspace_snapshot import WorkspaceSnapshotError, create_workspace_snapshot


@pytest.fixture
def posix_command_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the legacy POSIX command corpus explicit on native Windows."""

    monkeypatch.setattr(controller_module, "native_shell_kind", lambda: "posix")
    monkeypatch.setattr(policy_module, "native_shell_kind", lambda: "posix")


def test_bello_state_initializes_required_files(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    assert store.path(EVENTS).exists()
    assert store.path(FINAL_REPORT).exists()
    assert store.get_bello_config().task_path == str(task)


def test_run_checkpoint_is_atomic_metadata_and_survives_resume_initialization(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    config = BelloConfig(project_root=str(tmp_path), task_path=str(task))
    store.initialize_bello(config, overwrite=True)
    checkpoint = {
        "version": 1,
        "phase": "completion_review",
        "state": "active",
        "workspace_path": str(tmp_path / "workspace"),
    }

    store.write_run_checkpoint(checkpoint)
    store.initialize_bello(config, mode="resume")

    assert store.path(RUN_CHECKPOINT).is_file()
    assert store.get_run_checkpoint() == checkpoint


def test_coder_thread_resume_params_restore_policy_and_multi_agent(tmp_path: Path) -> None:
    config = MultiAgentConfig(
        enabled=True,
        max_concurrent=3,
        default=SubagentDefaultConfig(
            model=MODEL_GPT_5_6_LUNA,
            intelligence="high",
        ),
        allowed={MODEL_GPT_5_6_LUNA: ("medium", "high")},
    )

    params = coder_thread_resume_params(
        "thread-1",
        tmp_path,
        model=MODEL_GPT_5_6_TERRA,
        multi_agent=config,
    )

    assert params["threadId"] == "thread-1"
    assert params["cwd"] == str(tmp_path.resolve())
    assert params["approvalPolicy"] == "on-request"
    assert params["approvalsReviewer"] == "user"
    assert params["model"] == MODEL_GPT_5_6_TERRA
    assert params["config"]["agents"]["enabled"] is True
    assert params["config"]["agents"]["max_concurrent_threads_per_session"] == 3


def test_internal_supervisor_dir_is_added_to_git_info_exclude(tmp_path: Path) -> None:
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude = git_info / "exclude"
    exclude.write_text("# local excludes\n", encoding="utf-8")

    _ensure_internal_runtime_git_excluded(tmp_path)
    _ensure_internal_runtime_git_excluded(tmp_path)

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".supervisor/") == 1
    assert lines.count(".supervisor") == 1


async def test_git_init_log_is_filtered_from_changed_files_source(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    (tmp_path / ".git-init.log").write_text("initial\n", encoding="utf-8")
    (tmp_path / "src.c").write_text("int value(void) { return 1; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "TASK.md", ".git-init.log", "src.c"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / ".git-init.log").write_text("initial\nmore git init output\n", encoding="utf-8")
    (tmp_path / "src.c").write_text("int value(void) { return 2; }\n", encoding="utf-8")

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.use_git_diff = True
    controller.observed_changed_files = {}

    paths = {file.path for file in await controller.changed_files()}
    diff_summary = await controller.diff_summary()

    assert paths == {"src.c"}
    assert "src.c" in diff_summary
    assert ".git-init.log" not in diff_summary


async def test_generated_cache_artifacts_are_filtered_from_changed_files_source(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "TASK.md", "src/app.c"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "src" / "app.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "src" / "app.o").write_bytes(b"\x7fELF\0object")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0\0\0pyc")
    (tmp_path / "compiler").write_bytes(b"\x7fELF\0compiled")
    script = tmp_path / "run_demo"
    script.write_text("#!/usr/bin/env bash\nprintf 'demo\\n'\n", encoding="utf-8")
    script.chmod(0o755)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.use_git_diff = True
    controller.observed_changed_files = {}

    changed = await controller.changed_files()
    paths = {file.path for file in changed}
    diff_summary = await controller.diff_summary()

    assert "src/app.c" in paths
    assert "run_demo" in paths
    assert "src/app.o" in paths
    assert "__pycache__/app.cpython-312.pyc" not in paths
    assert "compiler" in paths
    assert "src/app.c" in diff_summary
    assert "run_demo" in diff_summary
    assert "src/app.o" in diff_summary
    assert "__pycache__" not in diff_summary
    assert "compiler" in diff_summary

    controller.use_git_diff = False
    controller.observed_changed_files = {
        "src/app.c": ChangedFile(path="src/app.c", status="modified", sequence=2),
        "src/app.o": ChangedFile(path="src/app.o", status="modified", sequence=2),
        "__pycache__/app.cpython-312.pyc": ChangedFile(
            path="__pycache__/app.cpython-312.pyc",
            status="modified",
            sequence=2,
        ),
        "compiler": ChangedFile(path="compiler", status="modified", sequence=2),
    }

    observed_paths = {file.path for file in await controller.changed_files()}
    assert observed_paths == {"src/app.c", "src/app.o", "compiler"}


async def test_greenfield_untracked_files_keep_sequences_for_validation_freshness(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    task = tmp_path / "TASK.md"
    task.write_text("# Build a Python CLI", encoding="utf-8")
    subprocess.run(["git", "add", "TASK.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    source = tmp_path / "src" / "new module.py"
    test_file = tmp_path / "tests" / "test_cli.py"
    source.parent.mkdir()
    test_file.parent.mkdir()
    source.write_text("def main():\n    return 0\n", encoding="utf-8")
    test_file.write_text("def test_main():\n    assert True\n", encoding="utf-8")

    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = True
    controller.observed_changed_files = {
        "src/new module.py": ChangedFile(path="src/new module.py", status="modified", sequence=7),
        "tests/test_cli.py": ChangedFile(path="tests/test_cli.py", status="modified", sequence=8),
        "src/no-longer-changed.py": ChangedFile(path="src/no-longer-changed.py", status="modified", sequence=99),
    }

    changed = await controller.changed_files()
    by_path = {file.path: file for file in changed}

    assert set(by_path) == {"src/new module.py", "tests/test_cli.py"}
    assert by_path["src/new module.py"].status == "??"
    assert by_path["src/new module.py"].sequence == 7
    assert by_path["tests/test_cli.py"].status == "??"
    assert by_path["tests/test_cli.py"].sequence == 8

    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="2 passed", sequence=9)
    ]
    assert await controller._done_without_fresh_behavioral_validation() is None
    assert store.get_bello_config().last_relevant_edit_sequence == 8

    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="2 passed", sequence=8)
    ]
    stale_reason = await controller._done_without_fresh_behavioral_validation()
    assert stale_reason is not None
    assert "relevant edit sequence 8" in stale_reason


def test_coder_sandbox_defaults_to_workspace_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BELLO_CODER_SANDBOX", raising=False)

    assert coder_thread_params(tmp_path)["sandbox"] == "workspace-write"
    assert coder_turn_params("thread", "work", tmp_path)["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path.resolve())],
        "networkAccess": False,
    }


def test_coder_runtime_roots_do_not_escape_writable_workspace(tmp_path: Path) -> None:
    thread = coder_thread_params(tmp_path)
    turn = coder_turn_params("thread", "work", tmp_path)

    expected_roots = [str(tmp_path.resolve())]
    assert thread["runtimeWorkspaceRoots"] == expected_roots
    assert turn["runtimeWorkspaceRoots"] == expected_roots
    assert turn["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path.resolve())],
        "networkAccess": False,
    }


def test_snapshot_mode_protects_entire_original_workspace_from_approval_commands(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller._coder_snapshot = SimpleNamespace(original_root=tmp_path)

    assert controller._immutable_approval_paths() == (tmp_path, task)


def test_private_plan_canonical_source_remains_immutable(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    plan = tmp_path / "PLAN.md"
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.plan_path = plan
    controller._coder_snapshot = SimpleNamespace(original_root=tmp_path)

    assert controller._immutable_approval_paths() == (tmp_path, task, plan)


def test_workspace_write_preflight_rejects_network_or_extra_writable_roots(tmp_path: Path) -> None:
    valid = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False}
    same_root = {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path.resolve())],
        "networkAccess": False,
    }
    network_enabled = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": True}
    extra_root = {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path.parent.resolve())],
        "networkAccess": False,
    }

    assert _sandbox_matches_mode(valid, "workspace-write", workspace_root=tmp_path) is True
    assert _sandbox_matches_mode(same_root, "workspace-write", workspace_root=tmp_path) is True
    assert _sandbox_matches_mode(network_enabled, "workspace-write", workspace_root=tmp_path) is False
    assert _sandbox_matches_mode(extra_root, "workspace-write", workspace_root=tmp_path) is False


def test_coder_sandbox_can_use_read_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "read-only")

    assert coder_thread_params(tmp_path)["sandbox"] == "read-only"
    assert coder_turn_params("thread", "work", tmp_path)["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }


def test_coder_fast_mode_sets_codex_service_tier(tmp_path: Path) -> None:
    assert coder_thread_params(tmp_path)["serviceTier"] is None
    assert coder_turn_params("thread", "work", tmp_path)["serviceTier"] is None
    assert coder_thread_params(tmp_path, fast=True)["serviceTier"] == CODEX_FAST_SERVICE_TIER
    assert coder_turn_params("thread", "work", tmp_path, fast=True)["serviceTier"] == CODEX_FAST_SERVICE_TIER


def test_coder_turn_params_include_intelligence_effort(tmp_path: Path) -> None:
    assert coder_turn_params("thread", "work", tmp_path, intelligence="xhigh")["effort"] == "xhigh"


def test_coder_thread_disables_native_subagents_by_default(tmp_path: Path) -> None:
    params = coder_thread_params(tmp_path)

    assert params["config"] == {"agents": {"enabled": False}}
    assert "developerInstructions" not in params


def test_coder_thread_applies_structured_multi_agent_config_and_separate_instructions(tmp_path: Path) -> None:
    multi_agent = MultiAgentConfig(
        enabled=True,
        max_concurrent=6,
        default=SubagentDefaultConfig(MODEL_GPT_5_6_LUNA, "xhigh"),
        allowed={
            MODEL_GPT_5_6_LUNA: ("high", "xhigh"),
            MODEL_GPT_5_6_TERRA: ("medium", "high"),
        },
    )

    params = coder_thread_params(tmp_path, multi_agent=multi_agent)

    assert params["config"] == {
        "agents": {
            "enabled": True,
            "max_concurrent_threads_per_session": 6,
            "default_subagent_model": MODEL_GPT_5_6_LUNA,
            "default_subagent_reasoning_effort": "xhigh",
        }
    }
    instructions = params["developerInstructions"]
    assert instructions == build_multi_agent_developer_instructions(multi_agent)
    assert "fastest and least expensive allowed profile" in instructions
    assert f"- {MODEL_GPT_5_6_LUNA}: high, xhigh" in instructions
    assert f"- {MODEL_GPT_5_6_TERRA}: medium, high" in instructions
    assert "Wait for every subagent whose result affects task completion." in instructions
    assert "instructions" not in multi_agent.to_json_data()


def test_reviewer_multi_agent_instructions_keep_parent_judgment_and_choose_cheapest_profile() -> None:
    multi_agent = MultiAgentConfig(enabled=True)

    completion = build_multi_agent_developer_instructions(
        multi_agent,
        role="completion_review",
    )
    adversary = build_multi_agent_developer_instructions(
        multi_agent,
        role="adversary",
    )

    assert completion is not None
    assert "fastest and least expensive allowed profile" in completion
    assert "distinct requirements, modules, or validation questions" in completion
    assert "do not delegate the final judgment or final output" in completion
    assert "Independently verify relevant subagent findings" in completion
    assert "Keep delegation one level deep" in completion
    assert "Wait for every subagent whose result affects the final review" in completion
    assert adversary is not None
    assert "distinct attack surfaces, edge-case classes, or failure hypotheses" in adversary
    assert "do not delegate the final judgment or final output" in adversary


async def test_coder_session_passes_multi_agent_config_only_at_thread_start(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    multi_agent = MultiAgentConfig(enabled=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = None
            self.turn_params = None

        async def thread_start(self, params, *, timeout):
            self.thread_params = params
            return {"thread": {"id": "coder-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            return {"turn": {"id": "coder-turn"}}

    client = FakeClient()
    coder = CoderSession(
        client,
        store,
        tmp_path,
        task,
        multi_agent=multi_agent,
    )  # type: ignore[arg-type]

    await coder.start_thread()
    await coder.start_turn("unchanged user prompt")

    assert client.thread_params["config"]["agents"]["enabled"] is True
    assert client.thread_params["runtimeWorkspaceRoots"] == [str(tmp_path.resolve())]
    assert "developerInstructions" in client.thread_params
    assert client.turn_params["input"] == [
        {"type": "text", "text": "unchanged user prompt", "text_elements": []}
    ]
    assert "developerInstructions" not in client.turn_params
    assert "config" not in client.turn_params
    assert client.turn_params["runtimeWorkspaceRoots"] == [str(tmp_path.resolve())]


def test_git_status_path_parser_handles_missing_second_status_column() -> None:
    assert _path_from_git_status_line(" M public/src/admin/manage/users.js") == "public/src/admin/manage/users.js"
    assert _path_from_git_status_line("M  public/language/en-GB/admin/manage/users.json") == "public/language/en-GB/admin/manage/users.json"
    assert _path_from_git_status_line("M public/language/en-GB/admin/manage/users.json") == "public/language/en-GB/admin/manage/users.json"


def test_git_porcelain_z_parser_preserves_exact_paths_and_rename_destination() -> None:
    output = "R  src/new name.py\0src/old name.py\0?? new dir/file one.py\0"

    assert _git_status_entries_from_porcelain_v1_z(output) == [
        ("src/new name.py", "R"),
        ("new dir/file one.py", "??"),
    ]


async def test_changed_files_and_diff_summary_filter_internal_runtime_paths(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = True
    controller.observed_changed_files = {
        ".supervisor/CONFIG.json": ChangedFile(path=".supervisor/CONFIG.json", status="modified", sequence=1),
        "TASK.md": ChangedFile(path="TASK.md", status="modified", sequence=2),
        "src/app.py": ChangedFile(path="src/app.py", status="modified", sequence=3),
    }

    async def is_git_work_tree() -> bool:
        return True

    async def git_output(command):
        if command == ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]:
            return " M .supervisor/CONFIG.json\0 M TASK.md\0 M src/app.py\0"
        if command == ["git", "status", "--short"]:
            return " M .supervisor/CONFIG.json\n M TASK.md\n M src/app.py"
        if command == ["git", "diff", "--numstat", "HEAD", "--"]:
            return "1\t1\t.supervisor/CONFIG.json\n1\t0\tTASK.md\n2\t3\tsrc/app.py"
        if command == ["git", "diff", "--stat"]:
            return " .supervisor/CONFIG.json | 2 +-\n TASK.md | 1 +\n src/app.py | 5 ++---\n 3 files changed"
        if command == ["git", "diff", "--name-only"]:
            return ".supervisor/CONFIG.json\nTASK.md\nsrc/app.py"
        return None

    controller._is_git_work_tree = is_git_work_tree
    controller._git_output = git_output

    changed = await controller.changed_files()
    diff = await controller.diff_summary()

    assert [file.path for file in changed] == ["src/app.py"]
    assert ".supervisor" not in diff
    assert "TASK.md" not in diff
    assert "src/app.py" in diff


def test_file_kind_classifies_common_test_roots_before_source_extensions() -> None:
    assert _file_kind("test/user/emails.js") == "test"
    assert _file_kind("tests/test_flow.py") == "test"
    assert _file_kind("src/user/email.js") == "source"


def test_coder_sandbox_can_use_danger_full_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "danger-full-access")

    assert coder_thread_params(tmp_path)["sandbox"] == "danger-full-access"
    assert coder_turn_params("thread", "work", tmp_path)["sandboxPolicy"] == {"type": "dangerFullAccess"}


def test_bello_events_are_append_only_jsonl(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.append_event(AppEvent(sequence=1, source=AppEventSource.SYSTEM, event_type="test"))

    lines = store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["event_type"] == "test"



def test_fresh_initialization_creates_empty_previous_runs_without_run_slot(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")
    previous_runs = store.path(PREVIOUS_RUNS)

    assert previous_runs.is_dir()
    assert list(previous_runs.iterdir()) == []

    store.path(EVENTS).write_text('{"sequence": 9}\n', encoding="utf-8")
    store.path(LOG).write_text("old log\n", encoding="utf-8")
    (previous_runs / "run9").mkdir()
    (previous_runs / "run9" / "FINAL_REPORT.md").write_text("old report", encoding="utf-8")
    recovery = store.path(RECOVERY)
    (recovery / "run9" / "workspace").mkdir(parents=True)
    (recovery / "run9" / "workspace" / "app.py").write_text("recovery", encoding="utf-8")
    (store.state_dir / "scratch.txt").write_text("scratch", encoding="utf-8")

    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")

    assert store.path(EVENTS).read_text(encoding="utf-8") == ""
    assert store.path(LOG).read_text(encoding="utf-8") == ""
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert store.path(PREVIOUS_RUNS).is_dir()
    assert list(store.path(PREVIOUS_RUNS).iterdir()) == []
    assert not store.path(RECOVERY).exists()
    assert not (store.state_dir / "scratch.txt").exists()


def test_resume_initialization_preserves_history_and_resets_runtime_files(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    config = BelloConfig(project_root=str(tmp_path), task_path=str(task))
    store.initialize_bello(config, mode="fresh")
    previous_runs = store.path(PREVIOUS_RUNS)
    run1 = previous_runs / "run1"
    run1.mkdir()
    (run1 / "task.md").write_text("old task", encoding="utf-8")
    (run1 / "FINAL_REPORT.md").write_text("old report", encoding="utf-8")
    store.path(EVENTS).write_text('{"sequence": 42}\n', encoding="utf-8")
    store.path(LOG).write_text("old log\n", encoding="utf-8")
    store.path(FINAL_REPORT).write_text("stale final", encoding="utf-8")
    store.path(PROGRESS).write_text("stale progress", encoding="utf-8")
    store.path(DECISIONS).write_text("stale decisions", encoding="utf-8")
    store.path(SUPERVISOR_WAKES).write_text("stale wake\n", encoding="utf-8")
    store.path(RUNTIME_TRACE).write_text("stale trace\n", encoding="utf-8")
    store.path(RUNTIME_METRICS).write_text('{"old": true}\n', encoding="utf-8")
    recovery_workspace = store.path(RECOVERY) / "run2" / "workspace"
    recovery_workspace.mkdir(parents=True)
    (recovery_workspace / "app.py").write_text("recover me", encoding="utf-8")
    (store.state_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    (store.state_dir / "scratch_dir").mkdir()

    store.initialize_bello(config, mode="resume")

    assert store.path(EVENTS).read_text(encoding="utf-8") == '{"sequence": 42}\n'
    assert store.path(LOG).read_text(encoding="utf-8") == "old log\n"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "old task"
    assert (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "old report"
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert "not started" in store.path(PROGRESS).read_text(encoding="utf-8")
    assert store.path(DECISIONS).read_text(encoding="utf-8") == "# Decisions\n\n"
    assert store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8") == ""
    assert store.path(RUNTIME_TRACE).read_text(encoding="utf-8") == ""
    assert store.path(RUNTIME_METRICS).read_text(encoding="utf-8") == "{}\n"
    assert (recovery_workspace / "app.py").read_text(encoding="utf-8") == "recover me"
    assert not (store.state_dir / "scratch.txt").exists()
    assert not (store.state_dir / "scratch_dir").exists()


def test_archive_completed_run_copies_task_and_report_after_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")

    store.write_final_report("first report\n")
    run1 = store.archive_completed_run(task)
    store.write_final_report("second report\n")
    run2 = store.archive_completed_run(task)

    assert run1.name == "run1"
    assert run2.name == "run2"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "# Task"
    assert (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "first report\n"
    assert (run2 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "second report\n"


def test_controller_event_sequence_starts_at_one_when_events_are_empty(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task)
    controller.initialize_state()

    assert controller._sequence == 0

    controller._append_event(AppEventSource.SYSTEM, "test/new")

    lines = controller.store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["sequence"] == 1
    assert controller.store.get_bello_config().last_event_sequence == 1


def test_controller_event_sequence_continues_existing_events(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    store.append_event(AppEvent(sequence=7, source=AppEventSource.SYSTEM, event_type="old"))
    store.append_event(AppEvent(sequence=42, source=AppEventSource.SYSTEM, event_type="newer"))

    controller = BelloController(tmp_path, task_path=task)
    controller.initialize_state()

    assert controller._sequence == 42

    controller._append_event(AppEventSource.SYSTEM, "test/new")

    lines = controller.store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["sequence"] == 43
    assert controller.store.get_bello_config().last_event_sequence == 43


async def test_controller_stages_plan_only_in_disposable_coder_workspace(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("PRIVATE PLAN CONTENT\npytest -q\n", encoding="utf-8")
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    try:
        assert controller.plan_path == plan.resolve()
        assert controller.workspace_plan_path == snapshot.plan_path
        assert controller._active_coder_plan_path() == snapshot.plan_path
        assert snapshot.plan_path is not None
        assert snapshot.plan_path.read_text(encoding="utf-8") == (
            "PRIVATE PLAN CONTENT\npytest -q\n"
        )
        assert "plan_path" not in type(controller.store.get_bello_config()).model_fields
        for path in controller.store.state_dir.rglob("*"):
            if path.is_file():
                assert b"PRIVATE PLAN CONTENT" not in path.read_bytes()

        (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", "--", "PLAN.md"],
            cwd=snapshot.snapshot_root,
            check=True,
        )
        changed_files = await controller.changed_files()
        diff_summary = await controller.diff_summary()
        patch_summary = await controller.patch_summary()
        controller.validations = [
            ValidationRun(
                command="pytest -q",
                exit_code=0,
                passed=True,
                summary="pytest -q passed",
                captured_output="1 passed\n",
                sequence=1,
            ),
            ValidationRun(
                command="pytest tests/test_PLAN.md -q",
                exit_code=0,
                passed=True,
                summary="test_PLAN.md passed",
                captured_output="1 passed\n",
                sequence=2,
            ),
            ValidationRun(
                command="python -m pytest ./PLAN.md",
                exit_code=0,
                passed=True,
                summary="PRIVATE PLAN CONTENT",
                captured_output="PRIVATE PLAN CONTENT\n",
                sequence=3,
            )
        ]
        controller.inspections = [
            InspectionRun(
                command=f"cat {snapshot.plan_path}",
                exit_code=0,
                passed=True,
                summary="PRIVATE PLAN CONTENT",
                captured_output="PRIVATE PLAN CONTENT\n",
                sequence=4,
                inspected_paths=[str(snapshot.plan_path)],
            )
        ]
        packet_details = await controller.completion_packet_details(
            [*changed_files, ChangedFile(path="PLAN.md", status="added")]
        )
        review_payload = "\n".join(
            (
                diff_summary,
                patch_summary or "",
                repr(changed_files),
                repr(packet_details),
            )
        )
        assert "app.py" in review_payload
        assert str(snapshot.plan_path) not in review_payload
        assert "ChangedFile(path='PLAN.md'" not in review_payload
        assert "PRIVATE PLAN CONTENT" not in review_payload
        assert len(packet_details["validation_outputs"]) == 2
        assert packet_details["validation_outputs"][0].command == "pytest -q"
        assert packet_details["validation_outputs"][0].captured_output == "1 passed\n"
        assert packet_details["validation_outputs"][1].command == (
            "pytest tests/test_PLAN.md -q"
        )
        assert packet_details["inspection_outputs"] == []

        controller.store.append_text_locked(
            PROGRESS,
            f"- runtime quoted {snapshot.plan_path}\n",
        )
        controller.store.append_text_locked(
            DECISIONS,
            "- PRIVATE PLAN CONTENT\npytest -q\n",
        )
        controller.store.append_recent_action(
            f"runtime inspected {snapshot.plan_path}"
        )
        controller.store.append_recent_action(
            f"runtime resolved {snapshot.plan_source_path} from the private input"
        )
        controller.store.append_event(
            AppEvent(
                sequence=99,
                source=AppEventSource.SUPERVISOR,
                event_type="runtime/private_echo",
                payload={"text": "PRIVATE PLAN CONTENT\npytest -q\n"},
            )
        )
        review_agent = StatelessSupervisorAgent(  # type: ignore[arg-type]
            None,
            controller.store,
            task,
        )
        raw_packet = review_agent.build_packet(
            wake_sequence=100,
            current_summary=f"review after {snapshot.plan_path}",
        )
        safe_packet = controller._review_safe_packet_state(raw_packet)
        raw_state = json.dumps(raw_packet.model_dump(mode="json"))
        safe_state = json.dumps(safe_packet.model_dump(mode="json"))
        assert "PRIVATE PLAN CONTENT" in raw_state
        assert "PLAN.md" in raw_state
        assert "PRIVATE PLAN CONTENT" not in safe_state
        assert "PLAN.md" not in safe_state
        assert str(snapshot.plan_source_path) in raw_state
        assert str(snapshot.plan_source_path) not in safe_state
    finally:
        snapshot.cleanup()


def test_nested_plan_read_is_private_when_action_uses_its_local_cwd(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "notes" / "PLAN.md"
    plan.parent.mkdir()
    plan_text = "PRIVATE NESTED PLAN\n" + ("private-detail-" * 2000)
    plan.write_text(plan_text, encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    try:
        inspection = InspectionRun(
            command="cat PLAN.md",
            cwd=str(snapshot.snapshot_root / "notes"),
            exit_code=0,
            passed=True,
            summary="cat PLAN.md",
            captured_output=plan_text[:20_000],
            sequence=1,
            inspected_paths=["PLAN.md"],
        )

        assert controller._exposes_review_private_input(inspection) is True
        aggregate_read = InspectionRun(
            command='for f in notes/*.md; do cat "$f"; done',
            cwd=str(snapshot.snapshot_root),
            exit_code=0,
            passed=True,
            summary="read Markdown files",
            captured_output=plan_text[:20_000],
            sequence=2,
            inspected_paths=["notes/*.md"],
        )
        assert controller._exposes_review_private_input(aggregate_read) is True
        assert controller._review_safe_values([aggregate_read]) == []
    finally:
        snapshot.cleanup()


def test_private_plan_path_matching_respects_platform_case_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "ПЛАН.md"
    plan.write_text("PRIVATE PLAN\n", encoding="utf-8")
    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    validation = ValidationRun(
        command="pytest план.md -q",
        exit_code=0,
        passed=True,
        summary="lowercase план.md passed",
        captured_output="1 passed\n",
        sequence=1,
    )
    try:
        monkeypatch.setattr(controller_module, "is_windows_platform", lambda: False)
        assert controller._exposes_review_private_input(validation) is False
        monkeypatch.setattr(controller_module, "is_windows_platform", lambda: True)
        assert controller._exposes_review_private_input(validation) is True
        unrelated = validation.model_copy(
            update={
                "command": "pytest мегаплан.md -q",
                "raw_command": "pytest мегаплан.md -q",
                "normalized_command": "pytest мегаплан.md -q",
                "summary": "unrelated Cyrillic filename passed",
            }
        )
        assert controller._exposes_review_private_input(unrelated) is False
    finally:
        snapshot.cleanup()


def test_controller_clean_preserves_explicit_plan(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("private plan\n", encoding="utf-8")
    disposable = tmp_path / "remove-me.txt"
    disposable.write_text("remove\n", encoding="utf-8")

    BelloController(
        tmp_path,
        task_path=task,
        plan_path=plan,
        clean_workspace=True,
    )

    assert task.read_text(encoding="utf-8") == "# Task\n"
    assert plan.read_text(encoding="utf-8") == "private plan\n"
    assert not disposable.exists()


def test_controller_rejects_plan_without_disposable_coder_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("private plan\n", encoding="utf-8")
    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    monkeypatch.setattr(
        controller_module,
        "coder_sandbox_mode",
        lambda: "danger-full-access",
    )

    with pytest.raises(WorkspaceSnapshotError, match="workspace-write coder snapshot"):
        controller._prepare_coder_workspace()


def test_final_report_rendering(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.write_final_report(FinalReport(task_path=str(task), status="complete", result="done", files_changed=["a.py"]))

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "# Final Report" in text
    assert "- a.py" in text


def test_final_report_omits_completion_review_status_when_not_applicable(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.write_final_report(
        FinalReport(
            task_path=str(task),
            status="complete",
            result="completed normally",
            completion_review_accepted=None,
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "- Status: complete" in text
    assert "- Result: completed normally" in text
    assert "Completion review accepted" not in text


async def test_final_report_non_git_omits_git_usage_and_includes_validations(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = True
    controller.validations = [
        ValidationRun(command="pytest -q", exit_code=0, passed=True, summary="command completed: pytest -q exit=0", sequence=1)
    ]
    controller.observed_changed_files = {"cron.py": ChangedFile(path="cron.py", status="modified")}
    controller.tui = _FakeTUI()
    controller.running = True

    await controller.finalize("task complete")

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "usage: git diff" not in text
    assert "fatal: not a git repository" not in text
    assert "## Diff Summary" not in text
    assert "- cron.py" in text
    assert "- pytest -q (behavioral pass, exit=0)" in text

    run1 = store.path(PREVIOUS_RUNS) / "run1"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "# Task"
    archived_report = (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8")
    assert "# Final Report" in archived_report
    assert "- Result: task complete" in archived_report

    controller._archive_final_report_once()
    assert sorted(path.name for path in store.path(PREVIOUS_RUNS).iterdir()) == ["run1"]


async def test_finalize_applies_accepted_snapshot_patch_to_real_workspace(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.use_git_diff = True
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")

    await controller.finalize("task complete", status=BelloStatus.COMPLETE, completion_review_accepted=True)

    assert source.read_text(encoding="utf-8") == "value = 2\n"
    assert not snapshot.temp_root.exists()
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert "- app.py" in store.path(FINAL_REPORT).read_text(encoding="utf-8")


async def test_finalize_preserves_snapshot_and_escalates_when_patch_back_is_rejected(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.use_git_diff = True
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    await controller.finalize("task complete", status=BelloStatus.COMPLETE, completion_review_accepted=True)

    assert not (tmp_path / ".env").exists()
    assert not snapshot.temp_root.exists()
    recovery_workspace = tmp_path / ".supervisor" / "recovery" / "run1" / "workspace"
    assert recovery_workspace.is_dir()
    assert (recovery_workspace / ".env").read_text(encoding="utf-8") == "TOKEN=secret\n"
    assert not (recovery_workspace / ".git").exists()
    assert not (recovery_workspace / ".supervisor").exists()
    assert not (recovery_workspace / "TASK.md").is_symlink()
    assert (recovery_workspace / "TASK.md").read_text(encoding="utf-8") == "# Task"
    assert store.get_bello_config().status == BelloStatus.ESCALATED
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted snapshot could not be applied" in report
    assert "snapshot preserved" in report


async def test_noncomplete_run_preserves_workspace_without_applying_it(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller._coder_started = True
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")

    await controller.finalize("exited by user", status=BelloStatus.EXITED)

    recovery_workspace = tmp_path / ".supervisor" / "recovery" / "run1" / "workspace"
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert (recovery_workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (recovery_workspace / ".git").exists()
    assert not (recovery_workspace / ".supervisor").exists()
    assert store.get_bello_config().status == BelloStatus.EXITED
    assert str(recovery_workspace) in store.path(FINAL_REPORT).read_text(encoding="utf-8")


async def test_preflight_failure_cleans_unused_snapshot_without_recovery(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller._coder_started = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path

    await controller.finalize("preflight failed", status=BelloStatus.PROVIDER_FAILURE)

    assert not snapshot.temp_root.exists()
    assert not (store.state_dir / "recovery").exists()
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


@pytest.mark.skipif(os.name == "nt", reason="POSIX task-link integrity path")
def test_task_integrity_detects_replaced_snapshot_link(tmp_path: Path) -> None:
    controller, _store, _ = _runtime_controller(tmp_path)
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    try:
        assert controller._task_integrity_issue() is None
        snapshot.task_path.unlink()
        snapshot.task_path.write_text("weakened\n", encoding="utf-8")

        assert controller._task_integrity_issue() == "the coder workspace replaced or removed the read-only task link"
    finally:
        snapshot.cleanup()


def test_task_integrity_detects_replaced_snapshot_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _store, _ = _runtime_controller(tmp_path)
    monkeypatch.setattr(
        workspace_snapshot_module,
        "_runtime_exposure_mode",
        lambda: workspace_snapshot_module.RUNTIME_EXPOSURE_COPY,
    )
    monkeypatch.setattr(
        workspace_snapshot_module,
        "_native_windows_runtime_controls_enabled",
        lambda: False,
    )
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    try:
        assert not snapshot.task_path.is_symlink()
        assert controller._task_integrity_issue() is None
        snapshot.task_path.write_text("weakened\n", encoding="utf-8")

        assert controller._task_integrity_issue() == (
            "the coder workspace replaced or modified the isolated task copy"
        )
    finally:
        snapshot.cleanup()


async def test_runtime_git_inspection_waits_for_trusted_snapshot_config(tmp_path: Path) -> None:
    controller, _store, _ = _runtime_controller(tmp_path)
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    try:
        subprocess.run(
            ["git", "config", "--local", "filter.untrusted.clean", "false"],
            cwd=snapshot.snapshot_root,
            check=True,
        )

        assert await controller._git_output(["git", "status", "--short"]) is None

        repaired = controller._repair_snapshot_runtime_controls(source="test")

        assert repaired == ("git_config",)
        assert await controller._git_output(["git", "status", "--short"]) == ""
    finally:
        snapshot.cleanup()


def test_validation_ledger_classifies_static_and_behavioral_commands(
    posix_command_semantics: None,
) -> None:
    static_commands = [
        "/bin/zsh -lc 'node -c src/user/email.js'",
        "/bin/zsh -lc 'node --check src/user/email.js'",
        "npm run type-check",
        "pnpm run type-check",
        "yarn type-check",
        "npx tsc --noemit",
        "./node_modules/.bin/eslint src/user/email.js",
        "git diff --check",
    ]
    static_runs = [
        _validation_from_action(
            TriggeringAction(
                kind="commandExecution",
                command=command,
                exit_code=0,
                status="completed",
                summary=f"command completed: {command} exit=0",
            ),
            sequence=10 + index,
        )
        for index, command in enumerate(static_commands)
    ]
    behavioral = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/zsh -lc './node_modules/.bin/mocha test/user/emails.js'",
            exit_code=0,
            status="completed",
            summary="command completed: ./node_modules/.bin/mocha test/user/emails.js exit=0",
        ),
        sequence=11,
        item={"output": "  email confirmation\n    1 passing (12ms)\n"},
    )
    shell_node_test = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'node --test'",
            exit_code=0,
            status="completed",
            summary="command completed: /bin/bash -lc 'node --test' exit=0",
        ),
        sequence=12,
        item={"stdout": "ok 1 - mounted board\n1..1\n# tests 1\n# pass 1\n# fail 0\n"},
    )
    zero_tests = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="npm test",
            exit_code=0,
            status="completed",
            summary="command completed: npm test exit=0",
        ),
        sequence=12,
        item={"stdout": "Tests: 0 total\n"},
    )
    shell_zero_tests = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'npm test'",
            exit_code=0,
            status="completed",
            summary="command completed: /bin/bash -lc 'npm test' exit=0",
        ),
        sequence=12,
        item={"stdout": "Tests: 0 total\n"},
    )
    filtered = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="pytest tests/test_user.py::test_sends_email -k sends",
            exit_code=0,
            status="completed",
            summary="command completed: pytest tests/test_user.py::test_sends_email -k sends exit=0",
        ),
        sequence=13,
        item={"stdout": "tests/test_user.py::test_sends_email PASSED\n1 passed in 0.01s\n"},
    )
    filtered_same_identity = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="pytest tests/test_user.py::test_sends_email -k sends",
            exit_code=0,
            status="completed",
            summary="command completed: pytest tests/test_user.py::test_sends_email -k sends exit=0",
        ),
        sequence=99,
        item={"stdout": "tests/test_user.py::test_sends_email PASSED\n1 passed in 0.01s\n"},
    )
    broad_pytest = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="ANSIBLE_DEVEL_WARNING=False python -m pytest test/units/cli/test_galaxy.py test/units/galaxy/test_collection_install.py",
            exit_code=0,
            status="completed",
            summary="command completed: pytest broad target exit=0",
        ),
        sequence=15,
        item={"stdout": "============================= 155 passed in 5.45s =============================\n"},
    )
    broad_pytest_without_output = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="ANSIBLE_DEVEL_WARNING=False python -m pytest test/units/cli/test_galaxy.py test/units/galaxy/test_collection_install.py",
            exit_code=0,
            status="completed",
            summary="command completed: pytest broad target exit=0",
        ),
        sequence=16,
    )
    direct_script = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'python3 hello.py'",
            exit_code=0,
            status="completed",
            summary="command completed: /bin/bash -lc 'python3 hello.py' exit=0",
        ),
        sequence=14,
        item={"stdout": "hello world\n", "stderr": ""},
    )
    python_unittest = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'python3 -B -m unittest -v'",
            exit_code=0,
            status="completed",
            summary="command completed: /bin/bash -lc 'python3 -B -m unittest -v' exit=0",
        ),
        sequence=15,
        item={"stdout": "Ran 1 test in 0.001s\n\nOK\n"},
    )
    shell_visible_script = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc ./run_visible_tests.sh",
            exit_code=0,
            status="completed",
            summary="command completed: /bin/bash -lc ./run_visible_tests.sh exit=0",
        ),
        sequence=16,
        item={"stdout": "============================= 45 passed in 0.06s =============================\n"},
    )
    direct_visible_script = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="./run_visible_tests.sh",
            exit_code=0,
            status="completed",
            summary="command completed: ./run_visible_tests.sh exit=0",
        ),
        sequence=17,
        item={"stdout": "============================= 45 passed in 0.06s =============================\n"},
    )
    absolute_go_test = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="/usr/local/go/bin/go test -count=1 ./...",
            exit_code=0,
            status="completed",
            summary="command completed: /usr/local/go/bin/go test -count=1 ./... exit=0",
        ),
        sequence=18,
        item={"result": {"stdout": "ok github.com/example/project/core 0.02s\n"}},
    )

    assert all(run is not None and run.type == "static" and run.outcome == "pass" for run in static_runs)
    assert behavioral is not None
    assert behavioral.type == "behavioral"
    assert behavioral.outcome == "pass"
    assert shell_node_test is not None
    assert shell_node_test.type == "behavioral"
    assert shell_node_test.outcome == "pass"
    assert shell_node_test.trusted_validation_outcome == "passed"
    assert zero_tests is not None
    assert zero_tests.type == "behavioral"
    assert zero_tests.outcome == "fail"
    assert not zero_tests.passed
    assert shell_zero_tests is not None
    assert shell_zero_tests.type == "behavioral"
    assert shell_zero_tests.outcome == "fail"
    assert not shell_zero_tests.passed
    assert filtered is not None
    assert filtered_same_identity is not None
    assert filtered.validation_id.startswith("validation-")
    assert filtered.validation_id == filtered_same_identity.validation_id
    assert filtered.raw_command == "pytest tests/test_user.py::test_sends_email -k sends"
    assert filtered.normalized_command == "pytest tests/test_user.py::test_sends_email -k sends"
    assert filtered.trusted_validation_outcome == "passed"
    assert filtered.was_filtered is True
    assert "tests/test_user.py::test_sends_email" in filtered.executed_test_names
    assert filtered.executed_test_files == ["tests/test_user.py"]
    assert filtered.passed_count == 1
    assert filtered.failed_count == 0
    assert filtered.target_files_or_test_files == ["tests/test_user.py"]
    assert broad_pytest is not None
    assert broad_pytest.executed_test_names == [
        "test/units/cli/test_galaxy.py",
        "test/units/galaxy/test_collection_install.py",
    ]
    assert broad_pytest.executed_test_files == []
    assert broad_pytest.passed_count == 155
    assert broad_pytest.failed_count == 0
    assert broad_pytest_without_output is not None
    assert broad_pytest_without_output.executed_test_names == [
        "test/units/cli/test_galaxy.py",
        "test/units/galaxy/test_collection_install.py",
    ]
    assert broad_pytest_without_output.executed_test_files == []
    assert broad_pytest_without_output.passed_count is None
    assert broad_pytest_without_output.failed_count is None
    assert direct_script is not None
    assert direct_script.type == "behavior_demo"
    assert direct_script.captured_output == "hello world\n"
    assert direct_script.validation_id.startswith("validation-")
    assert python_unittest is not None
    assert python_unittest.type == "behavioral"
    assert python_unittest.trusted_validation_outcome == "passed"
    assert shell_visible_script is not None
    assert shell_visible_script.type == "behavioral"
    assert shell_visible_script.trusted_validation_outcome == "passed"
    assert shell_visible_script.passed_count == 45
    assert shell_visible_script.failed_count == 0
    assert direct_visible_script is not None
    assert direct_visible_script.type == "behavioral"
    assert direct_visible_script.passed_count == 45
    assert direct_visible_script.failed_count == 0
    assert absolute_go_test is not None
    assert absolute_go_test.type == "behavioral"
    assert absolute_go_test.passed is True
    assert "github.com/example/project/core" in absolute_go_test.captured_output
    assert _has_passing_behavioral_validation([*static_runs, behavioral, zero_tests, filtered, direct_script, shell_visible_script, direct_visible_script, absolute_go_test])


async def test_command_output_delta_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "delta": "hello "},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "delta": {"text": "world\n"}},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 hello.py",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )
    if controller._supervisor_task is not None:
        await controller._supervisor_task

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "python3 hello.py"
    assert validation.type == "behavior_demo"
    assert validation.passed is True
    assert "hello world" in validation.summary
    assert validation.captured_output == "hello world\n"
    assert controller._command_output_chunks == {}


async def test_plan_command_line_does_not_suppress_genuine_validation_event(
    tmp_path: Path,
) -> None:
    controller, _store, _fake, snapshot, _plan = _runtime_controller_with_plan(
        tmp_path,
        "PRIVATE_PLAN_SENTINEL_42\npytest -q\n",
    )
    try:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-plan-command",
                        "delta": "1 passed in 0.01s\n",
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-plan-command",
                        "item": {
                            "type": "commandExecution",
                            "command": "pytest -q",
                            "exitCode": 0,
                            "status": "completed",
                        },
                    },
                }
            )
        )
        if controller._supervisor_task is not None:
            await controller._supervisor_task

        assert len(controller.validations) == 1
        validation = controller.validations[0]
        assert validation.command == "pytest -q"
        assert validation.raw_command == "pytest -q"
        assert validation.type == "behavioral"
        assert validation.trusted_validation_outcome == "passed"
        assert validation.captured_output == "1 passed in 0.01s\n"
        assert controller._review_safe_values([validation]) == [validation]
        details = await controller.completion_packet_details([])
        assert len(details["validation_outputs"]) == 1
        assert details["validation_outputs"][0].command == "pytest -q"
    finally:
        snapshot.cleanup()


async def test_direct_plan_read_is_not_persisted_as_review_evidence(
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE_PLAN_SENTINEL_42"
    controller, store, _fake, snapshot, plan = _runtime_controller_with_plan(
        tmp_path,
        f"{sentinel}\npytest -q\n",
    )
    assert snapshot.plan_path is not None
    try:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-read-plan",
                        "delta": f"{sentinel}\npytest -q\n",
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-read-plan",
                        "item": {
                            "type": "commandExecution",
                            "command": f"cat {snapshot.plan_path}",
                            "cwd": str(snapshot.snapshot_root),
                            "exitCode": 0,
                            "status": "completed",
                        },
                    },
                }
            )
        )
        if controller._supervisor_task is not None:
            await controller._supervisor_task

        assert controller.validations == []
        assert controller.inspections == []
        assert controller._command_output_chunks == {}
        assert store.read_recent_actions(1) == ["workspace action completed"]
        details = await controller.completion_packet_details([])
        assert details["validation_outputs"] == []
        assert details["inspection_outputs"] == []
        forbidden = (
            sentinel.encode(),
            str(snapshot.plan_path).encode(),
            str(plan.resolve()).encode(),
        )
        for state_path in store.state_dir.rglob("*"):
            if state_path.is_file():
                payload = state_path.read_bytes()
                assert all(marker not in payload for marker in forbidden)
    finally:
        snapshot.cleanup()


async def test_non_coder_command_output_is_not_retained_or_added_to_ledger(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": "completion-thread",
                    "turnId": "completion-turn",
                    "itemId": "completion-command",
                    "delta": "large review output",
                },
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "completion-thread",
                    "turnId": "completion-turn",
                    "itemId": "completion-command",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_target.py",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )

    assert controller._command_output_chunks == {}
    assert controller.validations == []
    assert controller.inspections == []


async def test_camelcase_stdout_delta_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/stdoutDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "stdout": "ok pkg/a 0.01s\n"},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "/usr/local/go/bin/go test -count=1 ./...",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "/usr/local/go/bin/go test -count=1 ./..."
    assert validation.type == "behavioral"
    assert validation.passed is True
    assert validation.captured_output == "ok pkg/a 0.01s\n"
    assert "ok pkg/a" in validation.summary


async def test_command_aggregated_output_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 hello.py",
                        "cwd": str(tmp_path),
                        "exitCode": 0,
                        "status": "completed",
                        "aggregatedOutput": "Hello world\n",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "python3 hello.py"
    assert validation.type == "behavior_demo"
    assert validation.passed is True
    assert validation.captured_output == "Hello world\n"
    assert "Hello world" in validation.summary


def test_readiness_marker_detection_requires_own_exact_line() -> None:
    assert _has_readiness_marker("Summary\n  BELLO_READY_FOR_REVIEW  \n")
    assert not _has_readiness_marker("Summary BELLO_READY_FOR_REVIEW")
    assert not _has_readiness_marker("bello_ready_for_review")
    assert _has_malformed_readiness_marker("bello_ready_for_review")
    assert _has_malformed_readiness_marker("BELLO READY FOR REVIEW")
    assert not _has_malformed_readiness_marker("I am not emitting `BELLO_READY_FOR_REVIEW`.")


async def test_exact_marker_triggers_completion_review_accept(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    class CompletionSupervisor:
        def __init__(self) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.completion_packets = []

        def build_packet(self, **kwargs):
            packet = self.agent.build_packet(**kwargs)
            return packet

        async def decide(self, packet):
            raise AssertionError("runtime monitor should not handle exact marker")

        async def decide_completion(self, packet):
            self.completion_packets.append(packet)
            return CompletionReviewDecision(
                decision="accept",
                reason="fresh behavioral validation covers the task",
                files_reviewed=[
                    {"path": "TASK.md", "reason": "task contract", "kind": "other", "inspected": True, "limitation": None}
                ],
                behavior_evidence_matrix=[
                    {
                        "behavior": "task is complete",
                        "task_basis": "TASK.md",
                        "files_considered": ["TASK.md"],
                        "evidence": [
                            {
                                "validation_id": "validation-1",
                                "command": "pytest",
                                "sequence": 1,
                                "validation_type": "behavioral",
                                "outcome": "pass",
                                "freshness": "fresh",
                                "why_it_covers_behavior": "passes the submitted validation",
                            }
                        ],
                        "status": "covered",
                        "gap": None,
                    }
                ],
                uncovered_behaviors=[],
                validation_gaps=[],
                claim_evidence_mismatches=[],
                packet_or_access_limitations=[],
                changed_test_risks=[],
                message_to_coder=None,
                persistent_decision=None,
                progress_update="Completion review accepted final readiness.",
                clear_handoff=False,
                display_message=None,
                handoff=None,
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    fake = CompletionSupervisor()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = fake
    controller.pending_approvals = {}
    controller.last_coder_message = CoderMessage(
        text="Summary: done\nValidation: pytest\nBELLO_READY_FOR_REVIEW",
        sequence=1,
    )
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=1)
    ]
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._supervisor_task = None
    controller._last_completion_marker_sequence = None
    controller.no_marker_idle_nudge_count = 0
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.paused = False

    await controller._handle_coder_turn_completed(item_id="message-item")
    await controller._supervisor_task

    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_coder_message.text.endswith("BELLO_READY_FOR_REVIEW")
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted by completion_review" in report
    assert "- Completion review accepted: true" in report


async def test_summary_done_without_marker_steers_for_exact_marker_not_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"), overwrite=True)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = None
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.last_coder_message = CoderMessage(text="All tests pass. Done.", sequence=1)
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._supervisor_task = None
    controller.paused = False

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert controller.coder.messages == [NO_MARKER_IDLE_NUDGE]
    assert store.get_bello_config().status == BelloStatus.STARTING


@pytest.mark.parametrize(
    "phrase",
    [
        "material limitation",
        "validation limitation",
        "independent behavioral evidence is still missing",
        "independent behavioral evidence is missing",
        "independent evidence is still missing",
        "independent evidence is missing",
        "no untouched output-identified",
        "no compliant next validation step",
        "no compliant validation step",
        "cannot provide independent",
        "can't provide independent",
        "not ready under the independent-evidence requirement",
    ],
)
async def test_former_material_limitation_phrases_are_not_terminal(tmp_path: Path, phrase: str) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.interrupted = False

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

        async def interrupt(self) -> None:
            self.interrupted = True

    coder = FakeCoder()
    controller.coder = coder
    controller.last_coder_message = CoderMessage(
        text=f"I am not ready for review. Current constraint: {phrase}.",
        sequence=7,
    )
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={"active_coder_turn_id": None, "completion_review_enabled": False}
        )
    )

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert store.get_bello_config().status == BelloStatus.STARTING
    assert coder.messages == [NO_MARKER_IDLE_NUDGE]
    assert coder.interrupted is False
    assert "coder/material_limitation" not in store.path(EVENTS).read_text(encoding="utf-8")
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""


async def test_no_marker_idle_forces_completion_review_once(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None, "last_event_sequence": 17})
    )

    await controller._handle_no_marker_idle()
    await controller._supervisor_task

    assert len(fake.completion_packets) == 1
    assert controller.completion_returns[0].reason == "not used"
    assert "Controller forcing completion_review" in store.path(PROGRESS).read_text(encoding="utf-8")

    await controller._handle_no_marker_idle()

    assert len(fake.completion_packets) == 1


async def test_marker_with_completion_review_disabled_finalizes_without_review(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    controller.last_coder_message = CoderMessage(
        text="Summary: done\nValidation: pytest\nBELLO_READY_FOR_REVIEW",
        sequence=1,
    )
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=1)
    ]

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert fake.completion_packets == []
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "completion review disabled by config" in report
    assert "- Completion review accepted: false" in report
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "completion review is disabled by config" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "completion/review_disabled_finalize" for event in events)


async def test_completion_review_cli_override_beats_persisted_config(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    controller.completion_review = False
    assert controller._effective_completion_review() is False

    controller.completion_review = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    assert controller._effective_completion_review() is True

    controller.completion_review = None
    assert controller._effective_completion_review() is False


async def test_completion_review_disabled_suppresses_adversary(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.adversary_runs = None
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 2, "completion_review_enabled": False})
    )

    assert controller._effective_max_adversary_runs() == 0
    assert controller._adversary_model_required_for_preflight() is False


async def test_no_marker_idle_nudges_coder_when_completion_review_disabled(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={"active_coder_turn_id": None, "last_event_sequence": 17, "completion_review_enabled": False}
        )
    )

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

    controller.coder = FakeCoder()

    await controller._handle_no_marker_idle()

    assert fake.completion_packets == []
    assert controller.coder.messages == [NO_MARKER_IDLE_NUDGE]


def test_runtime_supervisor_schema_rejects_complete() -> None:
    with pytest.raises(Exception):
        SupervisorDecision.model_validate({"decision": "complete"})


def test_validation_freshness_summary_marks_stale_behavioral_pass() -> None:
    summary = _validation_freshness_summary(
        validations=[
            ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=5),
        ],
        changed_files=[ChangedFile(path="app.py", status="modified", sequence=8)],
    )

    assert "behavioral validation is stale" in summary


async def test_runtime_noop_action_skips_supervisor_and_records_trace(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "pwd",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": str(tmp_path) + "\n",
                    },
                },
            }
        )
    )

    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["skipped_noop"] is True
    assert trace["should_wake_runtime_supervisor"] is False
    metrics = json.loads(store.path(RUNTIME_METRICS).read_text(encoding="utf-8"))
    assert metrics["runtime_skipped_noop_total"] == 1


async def test_first_isolated_nonzero_action_is_deterministic_noop(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 -c 'raise SystemExit(1)'",
                        "exitCode": 1,
                        "status": "completed",
                    },
                },
            }
        )
    )
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["should_wake_runtime_supervisor"] is False
    assert trace["trigger_reasons"] == []


def test_sole_nonzero_is_noop_even_with_an_older_unresolved_failure(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller.validation_runtime_state = {
        "older-validation": {
            "trusted_validation_outcome": "failed",
            "consecutive_failed_count": 2,
            "sequence": 2,
        }
    }

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python3 -c 'raise SystemExit(1)'",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[],
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


async def test_runtime_restart_budget_wakes_supervisor(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 -c 'print(1)'",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert "restart candidate because restart cap reached" in fake.runtime_packets[0].current_summary
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "restart_budget" in trace["trigger_reasons"]
    assert trace["restart_reason"] == "restart cap reached"


def test_restart_budget_wakes_once_per_health_state(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    duplicate = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    store.patch_health(
        lambda health: health.model_copy(
            update={"restart_count": 0, "risk_signals": ["bypass_after_denial"]}
        )
    )
    changed = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )

    assert first.reasons == ("restart_budget",)
    assert first.restart_reason == "restart cap reached"
    assert duplicate.should_wake is False
    assert changed.reasons == ("restart_budget",)
    assert changed.restart_reason == "bypass/rephrase attempt after denial"


def test_restart_budget_recurrence_after_clearing_is_new_state(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    first_batch = dict(controller._runtime_pending_trigger_signatures())

    store.patch_health(lambda health: health.model_copy(update={"restart_count": 0}))
    controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    controller._ack_runtime_trigger_batch(first_batch)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    recurring = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )

    assert first.reasons == ("restart_budget",)
    assert controller._last_restart_budget_signature is None
    assert recurring.reasons == ("restart_budget",)


def _runtime_failure_validation(
    *,
    sequence: int,
    output: str,
    validation_id: str = "validation-repeat",
    trusted_outcome: str = "failed",
    masking_reason: str | None = None,
    command: str = "pytest tests/test_parser.py",
) -> ValidationRun:
    passed = trusted_outcome == "passed"
    return ValidationRun(
        validation_id=validation_id,
        command=command,
        normalized_command=command,
        exit_code=0 if passed else 1,
        shell_exit_code=0 if passed else 1,
        outcome="pass" if passed else "fail",
        passed=passed,
        trusted_validation_outcome=trusted_outcome,
        masking_reason=masking_reason,
        summary=output,
        captured_output=output,
        sequence=sequence,
        executed_test_names=["tests/test_parser.py::test_parse"],
        executed_test_files=["tests/test_parser.py"],
        failed_count=0 if passed else 1,
    )


def _runtime_validation_packet(
    validation: ValidationRun,
    *,
    wake_sequence: int,
    reason: str = "repeated_same_failing_validation",
) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=wake_sequence,
        latest_event_sequence=wake_sequence,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary=f"Runtime trigger ({reason}): validation requires review",
        coder_thread_id="thread",
        triggering_action=TriggeringAction(
            kind="commandExecution",
            command=validation.command,
            exit_code=validation.exit_code,
            status="completed",
            summary=validation.summary,
        ),
        validations=[validation],
    )


def _runtime_unresolved_validation(
    *,
    sequence: int,
    command: str,
    validation_id: str,
) -> ValidationRun:
    return ValidationRun(
        validation_id=validation_id,
        command=command,
        normalized_command=command,
        exit_code=None,
        shell_exit_code=None,
        outcome="fail",
        passed=False,
        trusted_validation_outcome="failed",
        summary=f"command completed: {command} exit=None",
        sequence=sequence,
    )


def test_runtime_restart_issue_distinguishes_failures_and_ignores_legacy_masking() -> None:
    first = _runtime_failure_validation(sequence=1, output="AssertionError: expected 1, got 2")
    different = _runtime_failure_validation(sequence=2, output="ValueError: malformed header")

    first_issue = _runtime_restart_issue(_runtime_validation_packet(first, wake_sequence=1))
    different_issue = _runtime_restart_issue(_runtime_validation_packet(different, wake_sequence=2))

    assert first_issue is not None
    assert different_issue is not None
    assert first_issue.key != different_issue.key

    masked_a = _runtime_failure_validation(
        sequence=3,
        output="pipeline exit was masked",
        validation_id="validation-a",
        trusted_outcome="masked_or_unknown",
        masking_reason="shell_pipeline_masks_failure",
        command="bash strict-a.sh | tail",
    )
    masked_b = _runtime_failure_validation(
        sequence=4,
        output="different command masked the same way",
        validation_id="validation-b",
        trusted_outcome="masked_or_unknown",
        masking_reason="shell_pipeline_masks_failure",
        command="bash strict-b.sh | head",
    )

    masked_a_issue = _runtime_restart_issue(
        _runtime_validation_packet(masked_a, wake_sequence=3, reason="masked_validation")
    )
    masked_b_issue = _runtime_restart_issue(
        _runtime_validation_packet(masked_b, wake_sequence=4, reason="masked_validation")
    )

    assert masked_a_issue is None
    assert masked_b_issue is None


def test_runtime_restart_issue_groups_nested_shells_for_same_unresolved_command() -> None:
    direct = _runtime_unresolved_validation(
        sequence=1,
        command="/bin/bash -lc ./compile.sh",
        validation_id="validation-direct",
    )
    nested = _runtime_unresolved_validation(
        sequence=2,
        command="/bin/bash -c '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    different = _runtime_unresolved_validation(
        sequence=3,
        command="/bin/bash -lc ./test.sh",
        validation_id="validation-different",
    )

    direct_issue = _runtime_restart_issue(_runtime_validation_packet(direct, wake_sequence=1))
    nested_issue = _runtime_restart_issue(_runtime_validation_packet(nested, wake_sequence=2))
    different_issue = _runtime_restart_issue(
        _runtime_validation_packet(different, wake_sequence=3)
    )

    assert direct_issue is not None
    assert nested_issue is not None
    assert different_issue is not None
    assert direct_issue.key == nested_issue.key
    assert direct_issue.key != different_issue.key


def test_runtime_restart_issue_carries_active_failure_across_turn_completion() -> None:
    first = _runtime_unresolved_validation(
        sequence=10,
        command="/bin/bash -lc ./compile.sh",
        validation_id="validation-direct",
    )
    repeated = _runtime_unresolved_validation(
        sequence=12,
        command="/bin/bash -lc '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    active = _runtime_restart_issue(_runtime_validation_packet(first, wake_sequence=11))
    assert active is not None
    packet = SupervisorWakePacket(
        wake_sequence=13,
        latest_event_sequence=13,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary="Coder turn completed",
        coder_thread_id="thread",
        validations=[first, repeated],
    )

    carried = _runtime_restart_issue(
        packet,
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )
    stale = _runtime_restart_issue(
        packet,
        active_issue_key=active.key,
        active_issue_last_sequence=repeated.sequence,
    )
    different = _runtime_unresolved_validation(
        sequence=14,
        command="/bin/bash -lc ./test.sh",
        validation_id="validation-different",
    )
    superseded = _runtime_restart_issue(
        packet.model_copy(update={"validations": [first, repeated, different]}),
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )
    unrelated_wake = _runtime_restart_issue(
        packet.model_copy(
            update={"current_summary": "Runtime integrity trigger: runtime links restored."}
        ),
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )

    assert carried is not None
    assert carried.key == active.key
    assert carried.sequence == repeated.sequence
    assert stale is None
    assert superseded is None
    assert unrelated_wake is None


def test_runtime_event_issue_ignores_optional_file_change_action_metadata() -> None:
    base = SupervisorWakePacket(
        wake_sequence=20,
        latest_event_sequence=21,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary="Runtime trigger (large_diff): file change completed: 1 changes",
        coder_thread_id="thread",
        changed_files=[ChangedFile(path="src/parser.py", status="M", sequence=19)],
    )
    with_action = base.model_copy(
        update={
            "wake_sequence": 22,
            "latest_event_sequence": 23,
            "triggering_action": TriggeringAction(
                kind="fileChange",
                paths=["/tmp/coder/workspace/src/parser.py"],
                status="completed",
                summary="file change completed: 1 changes",
            ),
        }
    )
    different_path = with_action.model_copy(
        update={"changed_files": [ChangedFile(path="src/lexer.py", status="M", sequence=24)]}
    )

    base_issue = _runtime_restart_issue(base)
    action_issue = _runtime_restart_issue(with_action)
    different_issue = _runtime_restart_issue(different_path)

    assert base_issue is not None
    assert action_issue is not None
    assert different_issue is not None
    assert base_issue.key == action_issue.key
    assert base_issue.key != different_issue.key


async def test_runtime_restart_gate_counts_rejected_restart_as_steering_and_ignores_progress_update(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.steers: list[str] = []

        async def steer_or_start(self, message: str) -> None:
            self.steers.append(message)

    coder = FakeCoder()
    controller.coder = coder
    restarts: list[tuple[str, RestartHandoff | None]] = []

    async def capture_restart(reason: str, *, handoff: RestartHandoff | None = None) -> None:
        restarts.append((reason, handoff))

    controller.restart = capture_restart  # type: ignore[method-assign]
    handoff = RestartHandoff(
        objective="finish task",
        restart_reason="same failure repeated after steering",
        bad_pattern="rerunning the same failing validation",
        known_evidence="the same assertion failed repeatedly",
        next_step="inspect the assertion before editing",
        recovery_signal="the validation failure changes or passes",
    )

    for sequence in (1, 2, 3):
        event_sequence = sequence * 2 - 1
        wake_sequence = event_sequence + 1
        store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"last_event_sequence": event_sequence})
        )
        validation = _runtime_failure_validation(
            sequence=event_sequence,
            output="AssertionError: expected 1, got 2",
        )
        packet = _runtime_validation_packet(validation, wake_sequence=wake_sequence)
        if sequence == 1:
            decision = SupervisorDecision(
                decision=SupervisorDecisionKind.INTERVENE,
                reason="the same failure needs a controlled diagnostic",
                message_to_coder="Inspect the failing assertion before another edit.",
                progress_update="Recorded the first steering for this validation failure.",
                wake_sequence=wake_sequence,
                generation=0,
            )
        else:
            decision = SupervisorDecision(
                decision=SupervisorDecisionKind.RESTART,
                reason="coder repeated the same failure after steering",
                progress_update="Restart requested for the repeated validation failure.",
                handoff=handoff,
                wake_sequence=wake_sequence,
                generation=0,
            )
        await controller.apply_supervisor_decision(
            decision,
            packet_thread_id="thread",
            packet=packet,
        )

    assert len(coder.steers) == 2
    assert coder.steers[0] == "Inspect the failing assertion before another edit."
    assert "rerunning the same failing validation" in coder.steers[1]
    assert len(restarts) == 1
    assert restarts[0][0] == "coder repeated the same failure after steering"
    health = store.get_health()
    assert health.restart_issue_interventions == 2
    assert health.last_progress_sequence == 1
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert progress.count("Restart requested for the repeated validation failure.") == 1


def test_trusted_pass_clears_only_matching_runtime_restart_issue(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    failed = _runtime_failure_validation(sequence=1, output="AssertionError: expected 1, got 2")
    issue = _runtime_restart_issue(_runtime_validation_packet(failed, wake_sequence=1))
    assert issue is not None
    controller._record_runtime_intervention(
        reason="first steering",
        message="inspect the failure",
        sequence=1,
        generation=0,
        issue=issue,
    )

    unrelated_pass = _runtime_failure_validation(
        sequence=2,
        output="1 passed",
        validation_id="validation-other",
        trusted_outcome="passed",
    )
    controller._record_validation_runtime_state(unrelated_pass)
    assert store.get_health().restart_issue_key == issue.key

    matching_pass = unrelated_pass.model_copy(
        update={"validation_id": failed.validation_id, "sequence": 3}
    )
    controller._record_validation_runtime_state(matching_pass)
    assert store.get_health().restart_issue_key is None


def test_trusted_pass_clears_unresolved_issue_through_equivalent_shell_wrapper(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    unresolved = _runtime_unresolved_validation(
        sequence=1,
        command="/bin/bash -lc '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    issue = _runtime_restart_issue(_runtime_validation_packet(unresolved, wake_sequence=1))
    assert issue is not None
    controller._record_runtime_intervention(
        reason="build did not execute",
        message="run the build once through the normal approval path",
        sequence=1,
        generation=0,
        issue=issue,
    )

    passed = unresolved.model_copy(
        update={
            "validation_id": "validation-direct",
            "command": "/bin/bash -lc ./compile.sh",
            "normalized_command": "/bin/bash -lc ./compile.sh",
            "exit_code": 0,
            "shell_exit_code": 0,
            "outcome": "pass",
            "passed": True,
            "trusted_validation_outcome": "passed",
            "summary": "command completed: /bin/bash -lc ./compile.sh exit=0",
            "sequence": 2,
        }
    )
    controller._record_validation_runtime_state(passed)

    assert store.get_health().restart_issue_key is None


@pytest.mark.parametrize(
    "reason",
    [
        "validation_regression",
        "repeated_same_failing_validation",
        "timeout",
        "suspicious_file_touched",
        "unknown_signal",
    ],
)
async def test_quality_runtime_wake_can_be_filtered_by_cheap_runtime(tmp_path: Path, reason: str) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    await controller._run_supervisor_check(
        f"Runtime trigger ({reason}): command completed: sed -n '1,120p' app.test.js exit=0",
        triggering_item_id="cmd-1",
        triggering_action=TriggeringAction(
            kind="commandExecution",
            command="sed -n '1,120p' app.test.js",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        human_message=None,
        patch_summary=None,
        completion_review=False,
    )

    assert len(cheap.calls) == 1
    assert cheap.calls[0].current_summary.startswith(f"Runtime trigger ({reason})")
    assert fake.runtime_packets == []


def test_cheap_runtime_switch_reads_persisted_runtime_config(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    assert controller._cheap_runtime_enabled() is True

    store.update_bello_config(lambda cfg: cfg.model_copy(update={"cheap_runtime": False}))

    assert controller._cheap_runtime_enabled() is False


@pytest.mark.parametrize(
    "summary",
    [
        "Runtime trigger (restart_budget): restart candidate because restart cap reached; command completed",
        "Runtime trigger (runtime_apply_retry): retry decision after apply failure",
        "Runtime trigger (runtime_control_replacement): coder workspace runtime links were restored",
        "Runtime trigger (runtime_decision_retry): refresh stale runtime decision",
        "Runtime integrity trigger: coder workspace runtime links were replaced and restored.",
    ],
)
async def test_mandatory_runtime_wake_bypasses_cheap_runtime_noop(tmp_path: Path, summary: str) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    await controller._run_supervisor_check(
        summary,
        triggering_item_id="message-1",
        triggering_action=None,
        human_message=None,
        patch_summary=None,
        completion_review=False,
    )

    assert cheap.calls == []
    assert len(fake.runtime_packets) == 1


def test_read_only_large_diff_trigger_is_suppressed_but_real_diff_change_wakes(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    read_only_action = TriggeringAction(
        kind="commandExecution",
        command="sed -n '1,20p' src/app.py",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    execution_action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    changed_files = [ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)]

    read_only = controller.should_wake_runtime_supervisor(
        action=read_only_action,
        validation=None,
        changed_files=changed_files,
    )
    first_execution = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=changed_files,
    )
    repeated_execution = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=changed_files,
    )
    changed_signature = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=601, deletions=0, sequence=2)],
    )

    assert read_only.should_wake is False
    assert read_only.reasons == ()
    assert first_execution.should_wake is True
    assert first_execution.reasons == ("large_diff",)
    assert repeated_execution.should_wake is False
    assert repeated_execution.reasons == ()
    assert changed_signature.should_wake is True
    assert changed_signature.reasons == ("large_diff",)


def test_suspicious_file_trigger_wakes_once_per_file_state(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    test_path = tmp_path / "tests" / "test_parser.py"
    test_path.parent.mkdir()
    test_path.write_text("assert parse('a') == 1\n", encoding="utf-8")
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    changed_files = [
        ChangedFile(path="tests/test_parser.py", status="M", additions=1, deletions=1, sequence=2)
    ]

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    unchanged = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    test_path.write_text("assert parse('b') == 2\n", encoding="utf-8")
    edited_again = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    cleaned = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    changed_after_clean = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )

    assert first.reasons == ("suspicious_file_touched",)
    assert unchanged.should_wake is False
    assert edited_again.reasons == ("suspicious_file_touched",)
    assert cleaned.should_wake is False
    assert changed_after_clean.reasons == ("suspicious_file_touched",)


def test_unchanged_suspicious_file_does_not_defeat_isolated_nonzero_noop(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    test_path = tmp_path / "tests" / "test_parser.py"
    test_path.parent.mkdir()
    test_path.write_text("assert parse('a') == 1\n", encoding="utf-8")
    changed_files = [ChangedFile(path="tests/test_parser.py", status="M", additions=1, deletions=0)]
    successful_action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    failing_action = successful_action.model_copy(update={"exit_code": 1})

    controller.should_wake_runtime_supervisor(
        action=successful_action,
        validation=None,
        changed_files=changed_files,
    )
    decision = controller.should_wake_runtime_supervisor(
        action=failing_action,
        validation=None,
        changed_files=changed_files,
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


def test_file_change_large_diff_wakes_runtime_triage_once(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed: src/app.py",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_project_execution_large_diff_wakes_runtime_supervisor(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'make -j4'",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_timeout_trigger_requires_explicit_structured_signal(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    textual = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python -c 'subprocess.run(cmd, timeout=30)'",
            exit_code=0,
            status="completed",
            summary="command mentions timeout but completed",
        ),
        validation=None,
        changed_files=[],
    )
    explicit = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python worker.py",
            exit_code=None,
            status="failed",
            timed_out=True,
            summary="command failed",
        ),
        validation=None,
        changed_files=[],
    )

    assert textual.should_wake is False
    assert textual.reasons == ()
    assert explicit.should_wake is True
    assert explicit.reasons == ("timeout",)


def test_project_execution_first_nonzero_is_deterministic_noop(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    action = TriggeringAction(
        kind="commandExecution",
        command="pytest tests/public/test_public.py",
        exit_code=1,
        status="completed",
        summary="command completed",
    )

    decision = controller.should_wake_runtime_supervisor(
        action=action,
        validation=ValidationRun(
            command=action.command or "",
            exit_code=1,
            type="behavioral",
            passed=False,
            summary="1 failed",
            trusted_validation_outcome="failed",
            sequence=3,
        ),
        changed_files=[],
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


def test_protected_runtime_reason_stays_visible_for_project_execution(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="pytest tests/public/test_public.py",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[],
        validation_trigger_reasons=("repeated_same_failing_validation",),
    )

    assert decision.should_wake is True
    assert decision.reasons == ("repeated_same_failing_validation", "nonzero_exit")


def test_unresolved_masked_validation_still_wakes_for_project_execution(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller.validation_runtime_state = {
        "validation-old": {
            "trusted_validation_outcome": "masked_or_unknown",
            "consecutive_failed_count": 0,
            "sequence": 2,
        }
    }

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="./run_visible_tests.sh",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        validation=ValidationRun(
            command="./run_visible_tests.sh",
            exit_code=0,
            type="behavioral",
            passed=True,
            summary="45 passed",
            trusted_validation_outcome="passed",
            sequence=4,
        ),
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=3)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_read_only_action_still_wakes_for_restart_budget(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="rg -n \"TODO\" src",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("restart_budget",)
    assert decision.restart_reason == "restart cap reached"


def test_pending_large_diff_trigger_survives_coalesced_turn_boundary(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    action = TriggeringAction(
        kind="fileChange",
        paths=["src/app.py"],
        status="completed",
        summary="file change completed",
    )

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    duplicate_while_queued = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    pending_batch = dict(controller._runtime_pending_trigger_signatures())
    prepared = controller._prepare_runtime_trigger_summary(
        "Coder turn completed",
        pending=pending_batch,
    )
    controller._ack_runtime_trigger_batch(pending_batch)
    after_review_started = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )

    assert first.reasons == ("large_diff",)
    assert duplicate_while_queued.should_wake is False
    assert prepared.startswith("Runtime trigger (large_diff):")
    assert "Coder turn completed" in prepared
    assert after_review_started.should_wake is False


def test_validation_regression_trigger_survives_coalesced_turn_boundary(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression, nonzero_exit): pytest exited 1"
    )
    pending_batch = dict(controller._runtime_pending_trigger_signatures())
    prepared = controller._prepare_runtime_trigger_summary(
        "Coder turn completed",
        pending=pending_batch,
    )

    assert prepared.startswith("Runtime trigger (validation_regression, nonzero_exit):")
    assert "Coder turn completed" in prepared


async def test_runtime_intervention_cancels_queued_completion_review(tmp_path: Path) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

    coder = FakeCoder()
    controller.coder = coder

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="concrete runtime correction",
            message_to_coder="Correct the runtime issue before declaring readiness.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (validation_regression): pytest regressed",
        None,
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []
    assert coder.messages == ["Correct the runtime issue before declaring readiness."]
    assert controller._supervisor_next_completion_summary is None


async def test_pause_closes_completion_review_session(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.pause()

    assert controller.paused is True
    assert fake.closed_completion_reviews == 1
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_runtime_no_message_retry_keeps_runtime_and_completion_in_separate_slots(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    recovered = await controller._handle_supervisor_no_message_failure(
        message="supervisor did not produce an agent message",
        summary="Runtime trigger (timeout): command timed out",
        completion_review=False,
    )

    assert recovered is True
    assert "Retry supervisor review" in (controller._supervisor_next_runtime_summary or "")
    assert controller._supervisor_next_completion_summary is not None
    assert controller._supervisor_next_completion_review is False
    assert controller._supervisor_next_summary == controller._supervisor_next_runtime_summary


async def test_repeated_runtime_timeout_blocks_stale_queued_completion(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class AlwaysTimeoutSupervisor:
        def __init__(self, state_store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, state_store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            raise SupervisorAgentError("runtime supervisor timed out")

    supervisor = AlwaysTimeoutSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (timeout): command execution timed out",
        "cmd-timeout",
        TriggeringAction(
            item_id="cmd-timeout",
            kind="commandExecution",
            command="pytest",
            status="failed",
            timed_out=True,
            summary="command execution timed out",
        ),
        None,
        None,
        False,
    )

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert controller._supervisor_next_completion_summary is None
    assert "refusing to run a stale completion review" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_queued_human_runtime_wake_preserves_full_context_and_bypasses_cheap(
    tmp_path: Path,
) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    human = HumanMessage(text="Discussion only; do not change files.", sequence=9)
    action = TriggeringAction(
        item_id="cmd-9",
        kind="commandExecution",
        command="pwd",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    controller._queue_supervisor_check(
        "Human message received",
        triggering_item_id="message-9",
        triggering_action=action,
        human_message=human,
        patch_summary="queued patch context",
        completion_review=False,
    )
    controller._queue_supervisor_check(
        "Coder turn completed",
        completion_review=False,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): initial event",
        None,
        None,
        None,
        None,
        False,
    )

    assert len(cheap.calls) == 1
    assert len(fake.runtime_packets) == 1
    packet = fake.runtime_packets[0]
    assert packet.current_summary == "Human message received"
    assert packet.human_message == human
    assert packet.triggering_item_id == "message-9"
    assert packet.triggering_action == action
    assert packet.patch_summary == "queued patch context"


async def test_stale_runtime_decision_does_not_ack_trigger_signature(tmp_path: Path) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed",
        ),
        validation=None,
        changed_files=changed_files,
    )
    signature = controller._runtime_pending_trigger_signatures()["large_diff"][0]

    async def stale_decision(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            reason="stale generation",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation + 1,
        )

    fake.decide = stale_decision
    await controller._run_supervisor_check(
        "Runtime trigger (large_diff): file change completed",
        None,
        None,
        None,
        None,
        False,
    )

    assert decision.reasons == ("large_diff",)
    assert controller._last_large_diff_signature is None
    assert controller._runtime_pending_trigger_signatures()["large_diff"][0] == signature
    assert controller._supervisor_next_runtime_summary is not None


async def test_runtime_wake_arriving_during_completion_defers_completion_decision(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    original_completion = fake.decide_completion
    action = TriggeringAction(
        item_id="cmd-regression",
        kind="commandExecution",
        command="pytest",
        exit_code=1,
        status="failed",
        summary="pytest regressed",
    )

    async def completion_with_concurrent_runtime(packet):
        controller._queue_supervisor_check(
            "Runtime trigger (validation_regression): pytest regressed",
            triggering_item_id="cmd-regression",
            triggering_action=action,
            completion_review=False,
        )
        return await original_completion(packet)

    fake.decide_completion = completion_with_concurrent_runtime
    await controller._run_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        "message-ready",
        None,
        None,
        None,
        True,
    )

    assert store.get_bello_config().last_applied_supervisor_sequence == 0
    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_runtime_check.triggering_action == action
    assert controller._supervisor_next_completion_summary is not None
    assert controller._supervisor_next_completion_check.completion_review is True
    assert fake.closed_completion_reviews == 1


async def test_coalesced_runtime_reasons_retain_each_trigger_action_for_luna(
    tmp_path: Path,
) -> None:
    from supervisor.approval_triage import cheap_runtime_packet

    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    regression = TriggeringAction(
        item_id="pytest-1",
        kind="commandExecution",
        command="pytest tests/test_parser.py",
        exit_code=1,
        status="failed",
        summary="parser regression",
    )
    suspicious_edit = TriggeringAction(
        item_id="edit-2",
        kind="fileChange",
        paths=["tests/fixtures/parser.json"],
        status="completed",
        summary="fixture changed",
    )
    controller.validations = [
        ValidationRun(
            validation_id="parser-tests",
            command="pytest tests/test_parser.py",
            normalized_command="pytest tests/test_parser.py",
            exit_code=1,
            shell_exit_code=1,
            passed=False,
            trusted_validation_outcome="failed",
            summary="parser test failed",
            sequence=1,
        )
    ]
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression): parser regression",
        triggering_action=regression,
    )
    controller._queue_supervisor_check(
        "Runtime trigger (suspicious_file_touched): fixture changed",
        triggering_action=suspicious_edit,
        completion_review=False,
    )

    await controller._run_supervisor_check(
        "Runtime trigger (validation_regression): parser regression",
        "pytest-1",
        regression,
        None,
        None,
        False,
    )

    assert fake.runtime_packets == []
    assert len(cheap.calls) == 1
    packet = cheap.calls[0]
    assert set(packet.current_summary.split("(", 1)[1].split(")", 1)[0].split(", ")) == {
        "validation_regression",
        "suspicious_file_touched",
    }
    assert {action.item_id for action in packet.runtime_triggering_actions} == {
        "pytest-1",
        "edit-2",
    }
    slim = cheap_runtime_packet(packet)
    events = {event["action"]["item_id"]: event for event in slim["triggering_events"]}
    assert events["pytest-1"]["validation"]["validation_id"] == "parser-tests"
    assert events["edit-2"]["action"]["paths"] == ["tests/fixtures/parser.json"]
    assert controller._supervisor_next_runtime_summary is None


def test_ack_keeps_new_same_reason_trigger_when_other_queued_reason_is_covered(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    old_a = TriggeringAction(
        item_id="a-old",
        kind="commandExecution",
        command="pytest tests/test_a.py",
        exit_code=1,
        status="failed",
        summary="old A regression",
    )
    old_b = TriggeringAction(
        item_id="b-old",
        kind="commandExecution",
        command="pytest tests/test_b.py",
        exit_code=1,
        status="failed",
        summary="old B regression",
    )
    new_a = old_a.model_copy(update={"item_id": "a-new", "summary": "new A regression"})
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression): old A regression",
        triggering_action=old_a,
    )
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (repeated_same_failing_validation): old B regression",
        triggering_action=old_b,
    )
    old_batch = dict(controller._runtime_pending_trigger_signatures())
    controller._queue_supervisor_check(
        "Runtime trigger (validation_regression): new A regression",
        triggering_action=new_a,
        completion_review=False,
    )
    controller._queue_supervisor_check(
        "Runtime trigger (repeated_same_failing_validation): old B duplicate",
        triggering_action=old_b,
        completion_review=False,
    )

    controller._ack_runtime_trigger_batch(old_batch)

    assert list(controller._runtime_pending_trigger_signatures()) == ["validation_regression"]
    assert controller._runtime_pending_trigger_actions()["validation_regression"].item_id == "a-new"
    assert controller._supervisor_next_runtime_summary is not None
    assert "validation_regression" in controller._supervisor_next_runtime_summary
    assert controller._supervisor_next_runtime_check.triggering_action.item_id == "a-new"


async def test_failed_runtime_apply_requeues_trigger_and_cancels_stale_completion(
    tmp_path: Path,
) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)

    class FailingCoder:
        async def steer_or_start(self, message: str) -> str:
            raise RuntimeError("steering failed")

    controller.coder = FailingCoder()
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="runtime correction",
            message_to_coder="Correct the regression.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    await controller._run_supervisor_check(
        "Runtime trigger (validation_regression): pytest regressed",
        "cmd-regression",
        None,
        None,
        None,
        False,
    )

    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_completion_summary is None
    assert "validation_regression" in controller._runtime_pending_trigger_signatures()
    assert controller._runtime_apply_retry_count == 1


async def test_failed_runtime_steer_retries_same_wake_sequence_then_commits(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class InitialEscalatingCheapReviewer(_CheapRuntimeNoopReviewer):
        async def review(self, packet):
            self.calls.append(packet)
            return CheapRuntimeDecision(
                decision="escalate",
                reason_code="needs_supervisor_judgment",
            )

    cheap = InitialEscalatingCheapReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    class FlakyCoder:
        def __init__(self) -> None:
            self.attempts = 0
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.attempts += 1
            self.messages.append(message)
            if self.attempts == 1:
                raise RuntimeError("transient steering failure")
            return "turn"

    coder = FlakyCoder()
    controller.coder = coder
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="runtime correction",
            message_to_coder="Correct the regression.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    await controller._supervisor_check_loop(
        "Runtime trigger (validation_regression): pytest regressed",
        "cmd-regression",
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 2
    assert [packet.wake_sequence for packet in fake.runtime_packets] == [1, 1]
    assert coder.attempts == 2
    assert coder.messages == ["Correct the regression.", "Correct the regression."]
    assert store.get_bello_config().last_applied_supervisor_sequence == 1
    assert controller._runtime_pending_trigger_signatures() == {}
    assert controller._supervisor_next_completion_summary is None
    assert controller._runtime_apply_retry_count == 0
    assert len(cheap.calls) == 1


async def test_repeated_stale_runtime_decision_fails_bounded_without_completion(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class InitialEscalatingCheapReviewer(_CheapRuntimeNoopReviewer):
        async def review(self, packet):
            self.calls.append(packet)
            return CheapRuntimeDecision(
                decision="escalate",
                reason_code="needs_supervisor_judgment",
            )

    cheap = InitialEscalatingCheapReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def stale(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            reason="stale generation",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation + 1,
        )

    fake.decide = stale
    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): file change completed",
        "file-1",
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 2
    assert len(cheap.calls) == 1
    assert controller._runtime_decision_retry_count == 1
    assert controller._supervisor_next_completion_summary is None
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


async def test_runtime_pause_discards_queued_reviews_and_stops_queue_loop(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def pause(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.PAUSE,
            reason="human-only input required",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = pause
    await controller._supervisor_check_loop(
        "Human message to supervisor: wait for credentials",
        None,
        None,
        HumanMessage(text="wait for credentials", sequence=1),
        None,
        False,
    )

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []
    assert controller.paused is True
    assert controller._supervisor_next_runtime_summary is None
    assert controller._supervisor_next_completion_summary is None
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_external_pause_discards_inflight_runtime_decision(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.interrupted = False

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

        async def interrupt(self) -> None:
            self.interrupted = True

    coder = FakeCoder()
    controller.coder = coder

    async def intervene_after_external_pause(packet):
        fake.runtime_packets.append(packet)
        await controller.pause()
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="late pre-pause correction",
            message_to_coder="This must not be delivered after pause.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene_after_external_pause
    await controller._supervisor_check_loop(
        "Human message to supervisor: inspect current state",
        None,
        None,
        HumanMessage(text="inspect current state", sequence=1),
        None,
        False,
    )

    assert coder.interrupted is True
    assert coder.messages == []
    assert store.get_bello_config().last_applied_supervisor_sequence == 0
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_shell_command_shape_does_not_create_masked_validation_wake(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_app.py | cat",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "tests/test_app.py::test_app PASSED\n1 passed in 0.01s\n",
                    },
                },
            }
        )
    )
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    assert controller.validations[0].trusted_validation_outcome == "passed"
    assert controller.validations[0].masking_reason is None
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "masked_validation" not in trace["trigger_reasons"]


async def test_test_runner_failure_output_is_failed_without_masked_gate(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-failed",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_app.py | cat",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "tests/test_app.py::test_app FAILED\n1 failed in 0.01s\n",
                    },
                },
            }
        )
    )

    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    validation = controller.validations[0]
    assert validation.outcome == "fail"
    assert validation.passed is False
    assert validation.trusted_validation_outcome == "failed"
    assert validation.masking_reason is None
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["trigger_reasons"] == []


async def test_repeated_same_failing_validation_uses_command_identity(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    item = {
        "type": "commandExecution",
        "command": "pytest tests/test_app.py",
        "exitCode": 1,
        "status": "completed",
        "stdout": "tests/test_app.py::test_app FAILED\n1 failed in 0.01s\n",
    }

    await controller.handle_notification(
        AppServerMessage({"method": "item/completed", "params": {"threadId": "thread", "itemId": "cmd-1", "item": item}})
    )
    assert controller._supervisor_task is None
    await controller.handle_notification(
        AppServerMessage({"method": "item/completed", "params": {"threadId": "thread", "itemId": "cmd-2", "item": item}})
    )
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert controller.validations[0].validation_id == controller.validations[1].validation_id
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "repeated_same_failing_validation" in trace["trigger_reasons"]


def _prepare_done_without_fresh_validation(controller: BelloController) -> None:
    controller.last_coder_message = CoderMessage(text="Summary\nBELLO_READY_FOR_REVIEW", sequence=3)
    controller.observed_changed_files = {
        "src/app.py": ChangedFile(path="src/app.py", status="modified", sequence=2)
    }
    controller.validations = [
        ValidationRun(
            command="node --check src/app.js",
            exit_code=0,
            type="static",
            passed=True,
            summary="ok",
            sequence=3,
        )
    ]


async def test_done_without_fresh_validation_runtime_noop_resumes_completion(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-1")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.runtime_packets[0].current_summary.startswith(
        "Runtime trigger (done_without_fresh_validation):"
    )
    assert cheap.calls == []
    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_readiness_marker_sequence == 3
    assert fake.completion_packets[0].wake_sequence > fake.runtime_packets[0].wake_sequence
    assert len(controller.completion_returns) == 1
    assert store.get_bello_config().last_relevant_edit_sequence == 2
    assert "completion/readiness_validation_waived" in store.path(EVENTS).read_text(encoding="utf-8")
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["trigger_reasons"] == ["done_without_fresh_validation"]
    assert trace["should_wake_runtime_supervisor"] is True
    assert trace["deterministic_action"] is None
    assert trace["skipped_noop"] is False


async def test_done_without_fresh_validation_runtime_noop_ignores_reviewer_notifications(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    reviewer_thread = "runtime-reviewer-thread"
    fake.runtime_thread_id = reviewer_thread

    async def append_reviewer_notifications() -> None:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "thread/started",
                    "params": {
                        "thread": {
                            "id": reviewer_thread,
                            "status": {"type": "active"},
                        }
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": reviewer_thread,
                        "turn": {"id": "runtime-reviewer-turn"},
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": reviewer_thread,
                        "turnId": "runtime-reviewer-turn",
                        "item": {
                            "id": "runtime-reviewer-message",
                            "type": "agentMessage",
                            "text": '{"decision":"noop"}',
                        },
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {"method": "account/rateLimits/updated", "params": {}}
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": reviewer_thread,
                        "turn": {"id": "runtime-reviewer-turn"},
                    },
                }
            )
        )

    fake.before_runtime_decision = append_reviewer_notifications

    await controller._handle_coder_turn_completed(item_id="done-reviewer-events")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert reviewer_thread in controller._reviewer_thread_ids
    assert store.get_bello_config().last_event_sequence > fake.runtime_packets[0].latest_event_sequence
    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_readiness_marker_sequence == 3


@pytest.mark.parametrize(
    ("source", "event_type", "thread_id", "invalidates"),
    [
        (AppEventSource.APP_SERVER, "turn/started", "thread", True),
        (AppEventSource.APP_SERVER, "turn/started", "unknown-thread", True),
        (AppEventSource.APP_SERVER, "configWarning", None, True),
        (AppEventSource.USER, "user/input", None, True),
        (AppEventSource.SUPERVISOR, "controller/restart", None, True),
        (AppEventSource.APP_SERVER, "account/rateLimits/updated", None, False),
    ],
)
def test_readiness_snapshot_classifies_new_activity_fail_closed(
    tmp_path: Path,
    source: AppEventSource,
    event_type: str,
    thread_id: str | None,
    invalidates: bool,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    controller._append_event(source, event_type, thread_id=thread_id)

    assert (
        controller._readiness_snapshot_has_new_invalidating_event(
            packet,  # type: ignore[arg-type]
            cfg=store.get_bello_config(),
        )
        is invalidates
    )


async def test_readiness_snapshot_rejects_coder_descendant_activity(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "coder-child",
                        "parentThreadId": "thread",
                        "status": {"type": "active"},
                    }
                },
            }
        )
    )

    assert controller._is_coder_descendant("coder-child")
    assert controller._readiness_snapshot_has_new_invalidating_event(
        packet,  # type: ignore[arg-type]
        cfg=store.get_bello_config(),
    )


def test_readiness_snapshot_rejects_bounded_journal_coverage_gap(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    controller._readiness_event_journal_limit = 2
    reviewer_thread = "runtime-reviewer-thread"
    controller._register_reviewer_thread(reviewer_thread)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    for event_type in ("turn/started", "item/completed", "turn/completed"):
        controller._append_event(
            AppEventSource.APP_SERVER,
            event_type,
            thread_id=reviewer_thread,
        )

    assert len(controller._readiness_journal()) == 2
    assert controller._readiness_snapshot_has_new_invalidating_event(
        packet,  # type: ignore[arg-type]
        cfg=store.get_bello_config(),
    )


async def test_runtime_noop_rechecks_readiness_after_subagent_refresh(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    refresh_calls = 0

    async def refresh_with_late_user_activity() -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            controller._append_event(
                AppEventSource.USER,
                "user/input",
                reason="late activity during reviewer completion",
            )

    controller._refresh_coder_subagents = refresh_with_late_user_activity  # type: ignore[method-assign]

    await controller._handle_coder_turn_completed(item_id="done-refresh-race")
    await controller._supervisor_task

    assert refresh_calls == 2
    assert fake.completion_packets == []
    assert "completion/readiness_validation_waived" not in store.path(EVENTS).read_text(
        encoding="utf-8"
    )


async def test_done_without_fresh_validation_runtime_intervene_does_not_resume_completion(
    tmp_path: Path,
) -> None:
    controller, _, fake = _runtime_controller(tmp_path)
    fake.runtime_decision_kind = SupervisorDecisionKind.INTERVENE
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-intervene")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []


async def test_done_without_fresh_validation_runtime_noop_finalizes_when_review_disabled(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-review-disabled")
    await controller._supervisor_task

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert fake.completion_packets == []
    assert "completion/readiness_validation_waived" in store.path(EVENTS).read_text(encoding="utf-8")


async def test_done_without_fresh_validation_stale_runtime_noop_does_not_resume_completion(
    tmp_path: Path,
) -> None:
    controller, _, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    fake.before_runtime_decision = lambda: controller._append_event(
        AppEventSource.APP_SERVER,
        "test/newer_event",
    )

    await controller._handle_coder_turn_completed(item_id="done-stale")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []


async def test_completion_packet_details_can_send_delta_after_return(tmp_path: Path) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    controller.validations = [
        ValidationRun(command="pytest old.py", exit_code=0, passed=True, summary="old", sequence=1),
        ValidationRun(command="pytest new.py", exit_code=0, passed=True, summary="new", sequence=5),
    ]
    changed_files = [
        ChangedFile(path="src/old.py", status="M", sequence=2),
        ChangedFile(path="src/new.py", status="M", sequence=6),
    ]

    details = await controller.completion_packet_details(changed_files, since_sequence=3)

    assert [diff.path for diff in details["changed_file_diffs"]] == ["src/new.py"]
    assert [validation.validation_id for validation in details["validation_outputs"]] == [
        controller.validations[1].validation_id
    ]
    assert details["completion_delta_evidence_summary"] == [
        (
            f"validation {controller.validations[1].validation_id} seq=5 "
            "type=behavioral outcome=passed command=pytest new.py"
        )
    ]


def test_evidence_provenance_marks_changed_test_as_self_confirming() -> None:
    summary = _evidence_provenance_summary(
        validations=[
            ValidationRun(
                command="pytest tests/test_app_new.py",
                exit_code=0,
                passed=True,
                summary="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed",
                captured_output="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed\n",
                executed_test_files=["tests/test_app_new.py"],
                sequence=3,
            )
        ],
        changed_files=[
            ChangedFile(path="src/app.py", status="M", sequence=2),
            ChangedFile(path="tests/test_app_new.py", status="A", sequence=2),
        ],
        latest_change_sequence=2,
    )

    provenance = summary.validations[0]
    assert provenance.independence_class == "self_confirming"
    assert provenance.output_identifies_test_files is True
    assert provenance.coder_authored_test_files == ["tests/test_app_new.py"]
    assert provenance.untouched_executed_test_files == []
    assert provenance.risk_reasons == ["all_output_identified_tests_were_coder_authored"]


def test_evidence_provenance_canonicalizes_changed_tsx_test_reported_as_ts() -> None:
    summary = _evidence_provenance_summary(
        validations=[
            ValidationRun(
                command="npm test -- DeviceDetailHeading",
                exit_code=0,
                passed=True,
                summary="PASS src/components/DeviceDetailHeading-test.ts\n1 passed",
                captured_output="PASS src/components/DeviceDetailHeading-test.ts\n1 passed\n",
                executed_test_files=["src/components/DeviceDetailHeading-test.ts"],
                sequence=3,
            )
        ],
        changed_files=[
            ChangedFile(path="src/components/DeviceDetailHeading.tsx", status="M", sequence=2),
            ChangedFile(path="src/components/DeviceDetailHeading-test.tsx", status="A", sequence=2),
        ],
        latest_change_sequence=2,
    )

    provenance = summary.validations[0]
    assert provenance.independence_class == "self_confirming"
    assert provenance.executed_test_files == ["src/components/DeviceDetailHeading-test.ts"]
    assert provenance.coder_authored_test_files == ["src/components/DeviceDetailHeading-test.tsx"]
    assert provenance.untouched_executed_test_files == []


def test_evidence_provenance_marks_untouched_output_identified_test_as_independent() -> None:
    summary = _evidence_provenance_summary(
        validations=[
            ValidationRun(
                command="pytest tests/test_app_existing.py tests/test_app_new.py",
                exit_code=0,
                passed=True,
                summary=(
                    "tests/test_app_existing.py::test_requested_behavior PASSED\n"
                    "tests/test_app_new.py::test_requested_behavior PASSED\n2 passed"
                ),
                captured_output=(
                    "tests/test_app_existing.py::test_requested_behavior PASSED\n"
                    "tests/test_app_new.py::test_requested_behavior PASSED\n2 passed\n"
                ),
                executed_test_files=["tests/test_app_existing.py", "tests/test_app_new.py"],
                sequence=4,
            )
        ],
        changed_files=[
            ChangedFile(path="src/app.py", status="M", sequence=2),
            ChangedFile(path="tests/test_app_new.py", status="A", sequence=2),
        ],
        latest_change_sequence=2,
    )

    provenance = summary.validations[0]
    assert provenance.independence_class == "independent"
    assert provenance.coder_authored_test_files == ["tests/test_app_new.py"]
    assert provenance.untouched_executed_test_files == ["tests/test_app_existing.py"]
    assert provenance.risk_reasons == []


def test_evidence_provenance_classifies_behavior_demo_output() -> None:
    summary = _evidence_provenance_summary(
        validations=[
            ValidationRun(
                command="node -e \"console.log(render())\"",
                exit_code=0,
                type="behavior_demo",
                passed=True,
                summary="<button>Save</button>",
                captured_output="<button>Save</button>\n",
                sequence=3,
            ),
            ValidationRun(
                command="node -e \"console.log('PASS')\"",
                exit_code=0,
                type="behavior_demo",
                passed=True,
                summary="PASS",
                captured_output="PASS\n",
                sequence=4,
            ),
            ValidationRun(
                command="node -e \"runJest()\"",
                exit_code=0,
                type="behavior_demo",
                passed=True,
                summary="PASS src/App.test.tsx\n1 passed",
                captured_output="PASS src/App.test.tsx\n1 passed\n",
                sequence=5,
            ),
        ],
        changed_files=[ChangedFile(path="src/App.tsx", status="M", sequence=2)],
        latest_change_sequence=2,
    )

    factual, verdict, wrapped_test = summary.validations
    assert factual.independence_class == "independent_candidate"
    assert factual.output_kind == "factual_observation_candidate"
    assert verdict.independence_class == "not_independent"
    assert verdict.output_kind == "self_verdict_only"
    assert verdict.risk_reasons == ["behavior_demo_self_verdict_only"]
    assert wrapped_test.independence_class == "not_independent"
    assert wrapped_test.output_kind == "test_runner_output"
    assert wrapped_test.risk_reasons == ["behavior_demo_looks_like_test_runner_output"]


def test_heredoc_script_command_is_behavior_demo_validation(
    posix_command_semantics: None,
) -> None:
    command = "python - <<'PY'\nfrom app import render\nprint(render())\nPY"
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command=command,
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=7,
        item={"type": "commandExecution", "stdout": "<button>Save</button>\n"},
        changed_paths=["src/app.py"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.captured_output == "<button>Save</button>\n"


def test_absolute_python_script_command_is_behavior_demo_validation() -> None:
    command = f"{sys.executable} targeted_validation.py"
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command=command,
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=7,
        item={"type": "commandExecution", "stdout": "actual=42 expected=42\n"},
        changed_paths=["src/app.py"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.captured_output == "actual=42 expected=42\n"


def test_marked_behavior_demo_command_gets_validation_but_echo_is_rejected() -> None:
    demo = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 ./run_scenario src/app.py",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=8,
        item={"type": "commandExecution", "stdout": "rendered=<h1>Requested</h1>\n"},
        changed_paths=["src/app.py"],
    )
    echo = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 echo PASS",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=9,
        item={"type": "commandExecution", "stdout": "PASS\n"},
        changed_paths=["src/app.py"],
    )

    assert demo is not None
    assert demo.type == "behavior_demo"
    assert echo is None


def test_marked_behavior_demo_allows_honest_shell_sequence() -> None:
    command = (
        "BELLO_BEHAVIOR_DEMO=1 bash -lc 'set -euo pipefail; "
        "./bin/app --scenario smoke; printf \"scenario=smoke state=requested\\n\"'"
    )

    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command=command,
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=10,
        item={"type": "commandExecution", "stdout": "scenario=smoke state=requested\n"},
        changed_paths=["bin/app"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.masking_reason is None


def test_shell_shape_is_not_masked_but_output_quality_still_controls_evidence(
    posix_command_semantics: None,
) -> None:
    logical_or = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 bash -lc './bin/app --scenario smoke || true; echo PASS'",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=10,
        item={"type": "commandExecution", "stdout": "PASS\n"},
        changed_paths=["bin/app"],
    )
    pipeline = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario smoke | cat",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=11,
        item={"type": "commandExecution", "stdout": "scenario=smoke state=requested\n"},
        changed_paths=["bin/app"],
    )
    bare_pass = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 bash -lc './bin/app --scenario smoke; echo PASS'",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=12,
        item={"type": "commandExecution", "stdout": "PASS\n"},
        changed_paths=["bin/app"],
    )

    assert logical_or is not None
    assert logical_or.trusted_validation_outcome == "passed"
    assert logical_or.masking_reason is None
    assert _has_passing_behavioral_validation([logical_or]) is False
    assert pipeline is not None
    assert pipeline.trusted_validation_outcome == "passed"
    assert pipeline.masking_reason is None
    assert _has_passing_behavioral_validation([pipeline]) is True
    assert bare_pass is not None
    assert bare_pass.trusted_validation_outcome == "passed"
    assert bare_pass.masking_reason is None
    assert _has_passing_behavioral_validation([bare_pass]) is False


def test_validation_ledger_reads_aggregated_output_field() -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario smoke",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=10,
        item={"type": "commandExecution", "aggregatedOutput": "scenario=smoke state=requested\n"},
        changed_paths=["bin/app"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.captured_output == "scenario=smoke state=requested\n"


def test_command_output_aliases_are_attached_to_validation_ledger() -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario smoke",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=10,
        item={"type": "commandExecution", "aggregated_output": "scenario=smoke state=requested\n"},
        changed_paths=["bin/app"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.captured_output == "scenario=smoke state=requested\n"


def test_behavior_demo_without_real_output_is_recorded_but_not_usable_evidence() -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario smoke",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=10,
        item={"type": "commandExecution"},
        changed_paths=["bin/app"],
    )

    assert validation is not None
    assert validation.type == "behavior_demo"
    assert validation.outcome == "pass"
    assert validation.passed is True
    assert validation.trusted_validation_outcome == "passed"
    assert validation.masking_reason is None
    assert _has_passing_behavioral_validation([validation]) is False
    provenance = _evidence_provenance_summary(
        validations=[validation],
        changed_files=[ChangedFile(path="bin/app", status="M", sequence=2)],
        latest_change_sequence=2,
    ).validations[0]
    assert provenance.output_kind == "missing"
    assert provenance.independence_class == "not_independent"


def test_non_python_behavior_demo_commands_are_classified() -> None:
    cases = [
        (
            "node -e \"const app = require('./src/app'); console.log(app.render())\"",
            ["src/app.js"],
            "rendered=<h1>Requested</h1>\n",
        ),
        (
            "ruby -e \"require './src/app'; puts App.render\"",
            ["src/app.rb"],
            "rendered=<h1>Requested</h1>\n",
        ),
        (
            "curl -s http://localhost:3000/api/status",
            ["src/server.js"],
            '{"status":"ok","feature":"requested"}\n',
        ),
        (
            "BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario smoke",
            ["bin/app"],
            "scenario=smoke result=requested\n",
        ),
    ]

    for index, (command, changed_paths, output) in enumerate(cases, start=10):
        validation = _validation_from_action(
            TriggeringAction(
                kind="commandExecution",
                command=command,
                exit_code=0,
                status="completed",
                summary="command completed",
            ),
            sequence=index,
            item={"type": "commandExecution", "stdout": output},
            changed_paths=changed_paths,
        )

        assert validation is not None, command
        assert validation.type == "behavior_demo", command
        assert validation.captured_output == output


def test_supervisor_policy_has_no_specbench_split_triggers() -> None:
    root = Path(__file__).resolve().parents[1]
    texts = [
        (root / "supervisor" / "controller.py").read_text(encoding="utf-8"),
        (root / "supervisor" / "prompts" / "prompts.toml").read_text(encoding="utf-8"),
    ]
    forbidden = (
        "id" + "_private",
        "public + " + "id" + "_private",
        "public " + "green",
        "public " + "tests",
        "hidden " + "tests",
        "breadth_risk" + "_assessment",
    )

    for text in texts:
        lowered = text.lower()
        for token in forbidden:
            assert token not in lowered


def test_validation_output_prefers_test_runner_suite_files_over_stack_trace_paths() -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="npm test -- DeviceDetailHeading",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=8,
        item={
            "type": "commandExecution",
            "stdout": (
                "PASS src/components/DeviceDetailHeading-test.ts\n"
                "  at renderWithProviders (test/test-utils/utilities.ts:42:10)\n"
                "1 passed\n"
            ),
        },
        changed_paths=["src/components/DeviceDetailHeading.tsx"],
    )

    assert validation is not None
    assert validation.executed_test_files == ["src/components/DeviceDetailHeading-test.ts"]


def test_git_inspection_commands_are_not_behavioral_validations() -> None:
    diff_validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="git diff -- tests/test_app.py",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=8,
        item={"type": "commandExecution", "stdout": "diff --git a/tests/test_app.py b/tests/test_app.py\n"},
        changed_paths=["tests/test_app.py"],
    )
    check_validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution",
            command="git diff --check",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        sequence=9,
        item={"type": "commandExecution", "stdout": ""},
        changed_paths=["tests/test_app.py"],
    )

    assert diff_validation is None
    assert check_validation is not None
    assert check_validation.type == "static"


def test_read_only_test_file_commands_are_inspections_not_validations(
    posix_command_semantics: None,
) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command="sed -n '1,80p' tests/public/test_public.py",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    item = {"type": "commandExecution", "stdout": "def test_public():\n    assert app()\n"}

    validation = _validation_from_action(action, sequence=8, item=item, changed_paths=["tests/public/test_public.py"])
    inspection = _inspection_from_action(action, sequence=8, item=item)

    assert validation is None
    assert inspection is not None
    assert inspection.inspection_id.startswith("inspection-")
    assert inspection.passed is True
    assert inspection.inspected_paths == ["tests/public/test_public.py"]
    assert "def test_public" in inspection.captured_output


def test_shell_wrapped_read_only_test_file_commands_are_inspections_not_validations(
    posix_command_semantics: None,
) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command="/bin/bash -lc \"sed -n '1,80p' tests/public/test_public.py\"",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    item = {"type": "commandExecution", "stdout": "def test_public():\n    assert app()\n"}

    validation = _validation_from_action(action, sequence=8, item=item, changed_paths=["tests/public/test_public.py"])
    inspection = _inspection_from_action(action, sequence=8, item=item)

    assert validation is None
    assert inspection is not None
    assert inspection.passed is True
    assert inspection.inspected_paths == ["tests/public/test_public.py"]
    assert "def test_public" in inspection.captured_output


def test_forbidden_pattern_scan_with_regex_alternation_records_inspection(
    posix_command_semantics: None,
) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command='rg -n "system\\(|exec\\(|popen\\(" src include',
        exit_code=1,
        status="completed",
        summary="command completed",
    )
    item = {"type": "commandExecution", "stdout": ""}

    validation = _validation_from_action(action, sequence=8, item=item, changed_paths=["src/compiler.c"])
    inspection = _inspection_from_action(action, sequence=8, item=item)

    assert validation is None
    assert inspection is not None
    assert inspection.passed is True
    assert inspection.inspection_id.startswith("inspection-")
    assert inspection.inspected_paths == ["src", "include"]


@pytest.mark.parametrize(
    "command",
    [
        'PowerShell.EXE -NoProfile -Command "Write-Output pytest"',
        'PowerShell.EXE -NoProfile -Command "pytest tests; Write-Output passed"',
        'CMD.EXE /d /c "pytest tests & echo passed"',
        'CMD.EXE /d /c "type *"',
    ],
)
def test_ambiguous_windows_wrappers_do_not_become_validation_evidence(command: str) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(
        action,
        sequence=81,
        item={"type": "commandExecution", "stdout": "1 passed"},
        changed_paths=["src/app.py"],
    )
    inspection = _inspection_from_action(action, sequence=81)

    assert validation is None
    assert inspection is None


def test_simple_powershell_wrapper_records_behavioral_validation() -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command='PowerShell.EXE -NoProfile -Command "pytest tests/test_app.py -q"',
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(
        action,
        sequence=82,
        item={"type": "commandExecution", "stdout": "tests/test_app.py::test_flow PASSED\n1 passed"},
        changed_paths=["src/app.py"],
    )

    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.target_files_or_test_files == ["tests/test_app.py"]


async def test_literal_powershell_pythonpath_wrapper_records_usable_validation_and_trace(
    tmp_path: Path,
) -> None:
    # This is the exact quoting shape emitted for the successful Slab pytest
    # run on native Windows.  Approval remains fail-closed; only the completed
    # command's runtime-evidence classifier recognizes the literal prefix.
    command = (
        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        "-Command '$env:PYTHONPATH='\"'C:\\Users\\BelloSmoke\\AppData\\Local\\Temp\\"
        "slab-pytest-deps;src'; python -m pytest -q\""
    )
    wrapper = policy_module.windows_shell_wrapper_payload(command)
    assert wrapper is not None
    assert wrapper[0] == "powershell"
    assert wrapper[1] is None

    controller, store, _fake = _runtime_controller(tmp_path)
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-powershell-pythonpath",
                    "item": {
                        "type": "commandExecution",
                        "command": command,
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "5340 passed, 2 skipped in 52.01s\n",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.passed_count == 5340
    config = store.get_bello_config()
    assert config.last_validation_sequence == validation.sequence
    assert config.last_trusted_behavioral_validation_sequence == validation.sequence
    assert config.last_trusted_passing_behavioral_validation_sequence == validation.sequence
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["validation_type"] == "behavioral"
    assert trace["trusted_validation_outcome"] == "passed"


def test_direct_native_powershell_literal_pythonpath_records_behavioral_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller_module, "native_shell_kind", lambda: "powershell")
    action = TriggeringAction(
        kind="commandExecution",
        command=r"$env:PYTHONPATH='C:\deps;src'; py -3 -m pytest tests\test_app.py -q",
        exit_code=1,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(
        action,
        sequence=83,
        item={"type": "commandExecution", "stdout": "1 failed in 0.02s"},
        changed_paths=["src/app.py"],
    )

    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "failed"
    assert validation.target_files_or_test_files == ["tests/test_app.py"]


@pytest.mark.parametrize(
    "command",
    [
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; python -m pytest -q; exit 0"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; pytest | Out-Null"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; Write-Output '1 passed'"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTEST_ADDOPTS='--collect-only'; python -m pytest -q"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; node --version --test"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH=\"src;$env:SECRET\"; python -m pytest -q"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH=$(Get-Content path.txt); python -m pytest -q"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; $env:OTHER='x'; python -m pytest -q"''',
        r'''PowerShell.EXE -NoProfile -Command "Write-Output setup; $env:PYTHONPATH='src'; python -m pytest -q"''',
        r'''PowerShell.EXE -NoProfile -Command "$env:PYTHONPATH='src'; python -m pytest -q"; exit 0''',
        r'''PowerShell.EXE -Command "$env:PYTHONPATH='src"; exit 0; "'; python -m pytest -q"''',
    ],
)
def test_ambiguous_powershell_pythonpath_invocations_do_not_become_validation_evidence(
    command: str,
) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _validation_from_action(
        action,
        sequence=84,
        item={"type": "commandExecution", "stdout": "1 passed"},
        changed_paths=["src/app.py"],
    ) is None


@pytest.mark.parametrize(
    "option",
    [
        "--collect-only",
        "--co",
        "--help",
        "-h",
        "--version",
        "--version=2",
        "-V",
        "-VV",
        "-hh",
        "-hV",
        "-Vh",
        "-hfoo",
        "-qh",
        "-xh",
        "-vh",
        "-sh",
        "-lh",
        "-fh",
        "--fixtures",
        "--markers",
        "--setup-only",
        "--setup-plan",
    ],
)
def test_powershell_pythonpath_pytest_no_run_modes_are_not_validation_evidence(
    option: str,
) -> None:
    command = (
        'PowerShell.EXE -NoProfile -Command '
        f'"$env:PYTHONPATH=\'C:\\deps;src\'; python -m pytest {option}"'
    )
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _validation_from_action(
        action,
        sequence=85,
        item={"type": "commandExecution", "stdout": "12 tests collected"},
        changed_paths=["src/app.py"],
    ) is None


def test_direct_windows_pytest_collect_only_is_not_validation_evidence() -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command='PowerShell.EXE -NoProfile -Command "pytest --collect-only"',
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _validation_from_action(
        action,
        sequence=85,
        item={"type": "commandExecution", "stdout": "12 tests collected"},
        changed_paths=["src/app.py"],
    ) is None


@pytest.mark.parametrize(
    "command",
    [
        r'''PowerShell.EXE -Command "python -c 'print(1)' -m pytest"''',
        r'''PowerShell.EXE -Command "python --version -m pytest"''',
    ],
)
def test_python_action_before_module_is_not_usable_test_evidence(command: str) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(
        action,
        sequence=85,
        item={"type": "commandExecution", "stdout": "1 passed"},
        changed_paths=[],
    )

    assert validation is None or validation.type != "behavioral"
    assert _has_passing_behavioral_validation([validation] if validation is not None else []) is False


async def test_literal_powershell_file_wrapper_records_usable_validation_and_trace(tmp_path: Path) -> None:
    inner_command = (
        r"PowerShell.EXE -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        r"-File .\verify.ps1"
    )
    command = (
        r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" '
        rf'-Command "{inner_command}"'
    )
    # Approval analysis remains fail-closed for -File. Runtime evidence has a
    # separate literal-only recognizer after nested wrappers have executed.
    outer_wrapper = policy_module.windows_shell_wrapper_payload(command)
    inner_wrapper = policy_module.windows_shell_wrapper_payload(inner_command)
    assert outer_wrapper is not None
    assert outer_wrapper[1] == inner_command
    assert inner_wrapper is not None
    assert inner_wrapper[1] is None

    controller, store, _fake = _runtime_controller(tmp_path)
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-powershell-file",
                    "item": {
                        "type": "commandExecution",
                        "command": command,
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "VERIFY_OK junction\n",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.type == "behavior_demo"
    assert validation.trusted_validation_outcome == "passed"
    assert validation.captured_output == "VERIFY_OK junction\n"
    assert validation.target_files_or_test_files == ["verify.ps1"]
    assert _has_passing_behavioral_validation([validation]) is True
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["validation_type"] == "behavior_demo"
    assert trace["trusted_validation_outcome"] == "passed"


@pytest.mark.parametrize(
    "command",
    [
        r'PowerShell.EXE -NoProfile -File "$env:TEMP\verify.ps1"',
        r"PowerShell.EXE -NoProfile -File .\verify.ps1; Write-Output PASS",
        r"PowerShell.EXE -EncodedCommand AAAA",
        (
            r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" '
            r'-Command "powershell.exe -NoProfile -File .\verify.ps1; Write-Output PASS"'
        ),
    ],
)
def test_ambiguous_powershell_file_invocations_do_not_become_validation_evidence(command: str) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _validation_from_action(
        action,
        sequence=87,
        item={"type": "commandExecution", "stdout": "VERIFY_OK junction\n"},
        changed_paths=["app.py"],
    ) is None


def test_simple_cmd_wrapper_records_read_only_inspection() -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command='CMD.EXE /d /c "git status --short"',
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(action, sequence=83, changed_paths=[])
    inspection = _inspection_from_action(action, sequence=83)

    assert validation is None
    assert inspection is not None
    assert inspection.passed is True


@pytest.mark.parametrize(
    "command",
    [
        'PowerShell.EXE -NoProfile -Command "git branch new-branch"',
        'CMD.EXE /d /c "git remote add origin https://example.invalid/repo"',
    ],
)
def test_windows_git_mutations_do_not_become_inspection_evidence(command: str) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _inspection_from_action(action, sequence=84) is None


@pytest.mark.parametrize(
    "command",
    [
        'PowerShell.EXE -NoProfile -Command "py -3 -m pytest tests\\test_app.py -q"',
        'PowerShell.EXE -NoProfile -Command "python -X dev -m pytest tests\\test_app.py -q"',
        'PowerShell.EXE -NoProfile -Command "python -m pytest --trace-config tests\\test_app.py -q"',
        'CMD.EXE /d /c "npx.cmd vitest tests\\app.test.ts"',
    ],
)
def test_windows_python_launcher_and_npx_wrappers_record_behavioral_validation(command: str) -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command=command,
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    validation = _validation_from_action(
        action,
        sequence=85,
        item={"type": "commandExecution", "stdout": "1 passed"},
        changed_paths=["src/app.py"],
    )

    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "passed"


def test_windows_python_launcher_ambiguous_selector_is_not_validation_evidence() -> None:
    action = TriggeringAction(
        kind="commandExecution",
        command='PowerShell.EXE -NoProfile -Command "py -0p -m pytest tests"',
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    assert _validation_from_action(action, sequence=86, changed_paths=[]) is None


def test_windows_shell_wrapper_restart_key_matches_direct_payload() -> None:
    wrapped = 'PowerShell.EXE -NoProfile -Command "pytest tests/test_app.py -q"'

    assert _canonical_restart_command(wrapped) == _canonical_restart_command("pytest tests/test_app.py -q")


async def test_read_only_command_is_recorded_in_inspection_ledger(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "rg -n \"system\\(\" src",
                        "exitCode": 1,
                        "status": "completed",
                        "stdout": "",
                    },
                },
            }
        )
    )

    assert controller.validations == []
    assert len(controller.inspections) == 1
    assert controller.inspections[0].passed is True
    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["should_wake_runtime_supervisor"] is False


async def test_declared_grading_path_completed_command_escalates_integrity_failure(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    grading_root = tmp_path / "SpecBench" / "examples" / "c_compiler"
    grading_root.mkdir(parents=True)
    (grading_root / "hidden.c").write_text("int secret;\n", encoding="utf-8")
    controller.declared_grading_roots = (str(grading_root),)
    controller.approvals = ApprovalManager(tmp_path, declared_grading_roots=controller.declared_grading_roots)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": f"sed -n '1,20p' {grading_root / 'hidden.c'}",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "int secret;\n",
                    },
                },
            }
        )
    )

    assert store.get_bello_config().status == BelloStatus.ESCALATED
    assert controller.running is False
    assert fake.runtime_packets == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "coder accessed declared grading/hidden path" in progress


def test_evidence_provenance_marks_validation_before_latest_edit_as_stale() -> None:
    summary = _evidence_provenance_summary(
        validations=[
            ValidationRun(
                command="pytest tests/test_app_existing.py",
                exit_code=0,
                passed=True,
                summary="tests/test_app_existing.py::test_requested_behavior PASSED\n1 passed",
                captured_output="tests/test_app_existing.py::test_requested_behavior PASSED\n1 passed\n",
                executed_test_files=["tests/test_app_existing.py"],
                sequence=2,
            )
        ],
        changed_files=[ChangedFile(path="src/app.py", status="M", sequence=5)],
        latest_change_sequence=5,
    )

    provenance = summary.validations[0]
    assert provenance.fresh_after_latest_relevant_change is False
    assert provenance.independence_class == "stale"
    assert provenance.risk_reasons == ["stale_after_latest_relevant_change"]


async def test_completion_review_agent_reuses_thread_until_closed(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_starts = 0
            self.turn_starts = []
            self.archived = []

        async def thread_start(self, params, *, timeout):
            self.thread_starts += 1
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts.append(params["threadId"])
            return {
                "turn": {
                    "id": f"turn-{len(self.turn_starts)}",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "decision": "return",
                                    "reason": "needs more validation",
                                    "files_reviewed": [],
                                    "behavior_evidence_matrix": [],
                                    "uncovered_behaviors": ["fallback"],
                                    "validation_gaps": ["missing fallback test"],
                                    "claim_evidence_mismatches": [],
                                    "packet_or_access_limitations": [],
                                    "changed_test_risks": [],
                                    "message_to_coder": "validate fallback",
                                    "persistent_decision": None,
                                    "progress_update": None,
                                    "clear_handoff": False,
                                    "display_message": None,
                                    "handoff": None,
                                    "wake_sequence": 7,
                                    "generation": 0,
                                }
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    await agent.decide_completion(packet)
    await agent.decide_completion(packet)

    assert client.thread_starts == 1
    assert client.turn_starts == ["completion-thread", "completion-thread"]
    assert client.archived == []

    await agent.close_completion_review()

    assert client.archived == ["completion-thread"]


async def test_supervisor_fast_mode_sets_codex_service_tier(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = None
            self.turn_params = None

        async def thread_start(self, params, *, timeout):
            self.thread_params = params
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps({"decision": "noop", "reason": "routine progress"}),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    default_agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    assert default_agent._thread_params()["serviceTier"] is None

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task, fast=True)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="runtime check")

    decision = await agent.decide(packet)

    assert decision.decision == SupervisorDecisionKind.NOOP
    assert client.thread_params["serviceTier"] == CODEX_FAST_SERVICE_TIER
    assert client.turn_params["serviceTier"] == CODEX_FAST_SERVICE_TIER


async def test_supervisor_agent_sets_intelligence_effort(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.turn_params = None

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps({"decision": "noop", "reason": "routine progress"}),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task, intelligence="high")  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="runtime check")

    decision = await agent.decide(packet)

    assert decision.decision == SupervisorDecisionKind.NOOP
    assert client.turn_params["effort"] == "high"


async def test_completion_review_agent_overrides_stale_model_wake_sequence(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "decision": "return",
                                    "reason": "needs independent demo",
                                    "files_reviewed": [],
                                    "behavior_evidence_matrix": [],
                                    "uncovered_behaviors": ["rendered element"],
                                    "validation_gaps": ["missing factual demo output"],
                                    "claim_evidence_mismatches": [],
                                    "packet_or_access_limitations": [],
                                    "changed_test_risks": [],
                                    "message_to_coder": "provide a factual behavior_demo",
                                    "persistent_decision": None,
                                    "progress_update": None,
                                    "clear_handoff": False,
                                    "display_message": None,
                                    "handoff": None,
                                    "wake_sequence": 3,
                                    "generation": 99,
                                }
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=11, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.wake_sequence == 11
    assert decision.generation == 0
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["decision"]["wake_sequence"] == 11
    assert '"wake_sequence": 3' in audit["raw_text"]


async def test_completion_review_reads_assistant_message_content_from_turns_list(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    decision_text = json.dumps(
        {
            "decision": "return",
            "reason": "needs captured output",
            "files_reviewed": [],
            "behavior_evidence_matrix": [],
            "uncovered_behaviors": ["demo"],
            "validation_gaps": ["missing captured demo output"],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "record demo output",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 7,
            "generation": 0,
        }
    )

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "turn-1", "status": "completed", "items": []}}

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            assert limit == 5
            assert items_view == "full"
            return {
                "data": [
                    {"id": "older-turn", "items": [{"type": "agentMessage", "text": "{}"}]},
                    {
                        "id": "turn-1",
                        "items": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": decision_text}],
                            }
                        ],
                    },
                ]
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["status"] == "decision"
    assert audit["raw_text"] == decision_text


async def test_completion_review_no_message_retries_with_ultra_compact_minimal_prompt(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\nImplement the compiler.\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    valid_decision = {
        "decision": "return",
        "reason": "needs independent demo",
        "decision_artifact": {
            "current_state": "provider recovered on compact retry",
            "resolved_concerns": [],
            "stale_concerns": [],
            "uncovered_edge_candidates": ["independent demo missing"],
            "actionable_gap_or_none": "run an independent demo",
        },
        "files_reviewed": [],
        "behavior_evidence_matrix": [],
        "uncovered_behaviors": ["independent demo"],
        "validation_gaps": [],
        "claim_evidence_mismatches": [],
        "packet_or_access_limitations": [],
        "changed_test_risks": [],
        "message_to_coder": "Run an independent demo for the claimed compiler behavior.",
        "persistent_decision": None,
        "progress_update": None,
        "clear_handoff": False,
        "display_message": None,
        "handoff": None,
        "wake_sequence": 7,
        "generation": 0,
    }

    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0
            self.turn_inputs: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": f"completion-thread-{self.thread_count}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_inputs.append(params["input"][0]["text"])
            turn_number = len(self.turn_inputs)
            if turn_number <= 2:
                return {
                    "turn": {
                        "id": f"turn-{turn_number}",
                        "status": "completed",
                        "items": [],
                    }
                }
            return {
                "turn": {
                    "id": "turn-3",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": json.dumps(valid_decision)}],
                }
            }

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            return {"data": []}

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(
        wake_sequence=7,
        current_summary="completion review",
        validations=[
            ValidationRun(
                command="pytest tests/public",
                exit_code=0,
                passed=True,
                summary="46 passed\n" + ("x" * 5000),
                captured_output="46 passed\n" + ("y" * 5000),
                sequence=6,
            )
        ],
    )

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    assert len(client.turn_inputs) == 3
    assert "Emergency compact JSON retry" in client.turn_inputs[2]
    assert "ultra_compact_outputs" in client.turn_inputs[2]
    assert "supervisor did not produce an agent message" in client.turn_inputs[2]
    assert client.archived == ["completion-thread-1"]
    audit_rows = [
        json.loads(line)
        for line in store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_rows[-1]["use_case"] == "completion_review_no_message_minimal_retry"
    assert audit_rows[-1]["status"] == "decision"


async def test_supervisor_agent_retries_invalid_structured_output_once(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    valid_decision = {
        "decision": "return",
        "reason": "needs factual demo",
        "files_reviewed": [],
        "behavior_evidence_matrix": [],
        "uncovered_behaviors": ["demo"],
        "validation_gaps": ["missing factual demo output"],
        "claim_evidence_mismatches": [],
        "packet_or_access_limitations": [],
        "changed_test_risks": [],
        "message_to_coder": "record demo output",
        "persistent_decision": None,
        "progress_update": None,
        "clear_handoff": False,
        "display_message": None,
        "handoff": None,
        "wake_sequence": 7,
        "generation": 0,
    }

    class FakeClient:
        def __init__(self) -> None:
            self.turn_inputs = []

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_inputs.append(params["input"][0]["text"])
            if len(self.turn_inputs) == 1:
                return {
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"type": "agentMessage", "text": '{"decision":"return","reason":"unterminated'}],
                    }
                }
            return {
                "turn": {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": json.dumps(valid_decision)}],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    assert len(client.turn_inputs) == 2
    assert "previous completion-review response was not valid structured JSON" in client.turn_inputs[1]
    assert "compact completion-review JSON object" in client.turn_inputs[1]
    assert "files_reviewed=[]" in client.turn_inputs[1]
    assert "behavior_evidence_matrix=[]" in client.turn_inputs[1]
    assert "under 3000 characters" in client.turn_inputs[1]
    audits = [json.loads(line) for line in store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()]
    assert audits[-2]["use_case"] == "completion_review_parse_retry"
    assert audits[-2]["status"] == "error"
    assert audits[-1]["status"] == "decision"


async def test_terminal_state_denies_new_server_request_without_policy_path(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.tui = _FakeTUI()
    controller._terminal_cleanup_started = True

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 99,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "echo after terminal", "availableDecisions": ["accept", "decline"]},
            }
        )
    )

    assert controller.client.responses == [(99, {"decision": "decline"})]


async def test_completion_return_sends_message_and_continues_same_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0

    class _CloseTrackingSupervisor:
        def __init__(self) -> None:
            self.closed = 0

        async def close_completion_review(self) -> None:
            self.closed += 1

    runtime_supervisor = _CloseTrackingSupervisor()
    completion_supervisor = _CloseTrackingSupervisor()
    controller.supervisor = runtime_supervisor
    controller.completion_supervisor = completion_supervisor

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="return",
            reason="fallback behavior is uncovered",
            uncovered_behaviors=["missing-key fallback"],
            validation_gaps=["only happy path was validated"],
            message_to_coder="Validate missing-key fallback before marking ready again.",
            persistent_decision="Completion review requires fallback coverage.",
            progress_update="Completion review returned missing fallback coverage.",
            clear_handoff=False,
            display_message=None,
            handoff=None,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    assert store.get_bello_config().generation == 0
    assert controller.coder.messages == ["Validate missing-key fallback before marking ready again."]
    assert len(controller.completion_returns) == 1
    assert store.get_health().interventions == 0
    assert "Completion review returned missing fallback coverage" in store.path("PROGRESS.md").read_text(encoding="utf-8")
    # Fresh completion-review thread per review: a normal return closes the session so the
    # next readiness review starts a new thread instead of accumulating prior turns.
    assert runtime_supervisor.closed == 0
    assert completion_supervisor.closed == 1


@pytest.mark.parametrize(
    ("first_source", "first_completion_return_count"),
    [
        ("completion_review", 1),
        ("adversary_report_controller", 0),
    ],
)
async def test_review_returns_switch_once_and_reuse_revision_coder_thread(
    tmp_path: Path,
    first_source: str,
    first_completion_return_count: int,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("PRIVATE INITIAL IMPLEMENTATION PLAN\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            completion_review_enabled=True,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="xhigh",
        ),
        overwrite=True,
    )
    snapshot = create_workspace_snapshot(tmp_path, task, plan_path=plan)

    class RecordingClient:
        def __init__(self) -> None:
            self.thread_starts = []
            self.turn_starts = []
            self.turn_steers = []

        async def thread_start(self, params, *, timeout):
            self.thread_starts.append(params)
            return {"thread": {"id": f"revision-thread-{len(self.thread_starts)}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts.append(params)
            return {"turn": {"id": f"revision-turn-{len(self.turn_starts)}"}}

        async def turn_steer(self, thread_id, turn_id, message, *, timeout):
            self.turn_steers.append((thread_id, turn_id, message))
            return {}

    multi_agent = MultiAgentConfig(enabled=True)
    project_config = ProjectConfig(
        coder_mod=MODEL_GPT_5_6_SOL,
        coder_intelligence="ultra",
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="xhigh",
        completion_review=True,
        multi_agent=multi_agent,
    )
    client = RecordingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        snapshot.snapshot_root,
        snapshot.task_path,
        model=MODEL_GPT_5_6_SOL,
        intelligence="ultra",
        thread_id="initial-thread",
        multi_agent=multi_agent,
        plan_path=snapshot.plan_path,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.plan_path = plan
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = project_config
    controller.fast = True
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.completion_review_return_sequence = None
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="1 passed", sequence=2)
    ]
    original_validations = controller.validations
    pending_report = object()
    controller._pending_adversary_report = pending_report
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=3)
    controller._last_completion_marker_sequence = 3
    controller._no_marker_completion_review_key = "old"
    controller._deferred_completion_check = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_snapshot = snapshot
    controller._sequence = 3

    subprocess.run(
        ["git", "add", "-f", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        check=True,
    )
    staged_plan_blob = subprocess.run(
        ["git", "ls-files", "--stage", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.split()[1]

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    first_feedback = "Validate the missing-key fallback before reporting readiness again."
    first_decision = CompletionReviewDecision(
        decision="return",
        reason="fallback behavior is uncovered",
        uncovered_behaviors=["missing-key fallback"],
        validation_gaps=["only the happy path was validated"],
        message_to_coder=first_feedback,
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=4,
        generation=0,
    )

    await controller._return_completion_to_coder(first_decision, source=first_source)  # type: ignore[arg-type]

    config_after_switch = store.get_bello_config()
    first_prompt = client.turn_starts[0]["input"][0]["text"]
    assert len(client.thread_starts) == 1
    assert client.thread_starts[0]["model"] == MODEL_GPT_5_6_LUNA
    assert client.thread_starts[0]["serviceTier"] == CODEX_FAST_SERVICE_TIER
    assert client.thread_starts[0]["config"]["agents"]["enabled"] is True
    assert client.turn_starts[0]["threadId"] == "revision-thread-1"
    assert client.turn_starts[0]["model"] == MODEL_GPT_5_6_LUNA
    assert client.turn_starts[0]["effort"] == "xhigh"
    assert first_feedback in first_prompt
    assert ".supervisor/HANDOFF.md" in first_prompt
    assert ".supervisor/DECISIONS.md" in first_prompt
    assert ".supervisor/PROGRESS.md" in first_prompt
    assert first_prompt.count("BELLO_READY_FOR_REVIEW") == 1
    assert "on its own line" in first_prompt
    assert str(snapshot.plan_path) not in first_prompt
    assert "PRIVATE INITIAL IMPLEMENTATION PLAN" not in first_prompt
    assert snapshot.plan_exposed is False
    assert not snapshot.plan_path.exists()
    assert not snapshot.plan_path.is_symlink()
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).returncode != 0
    assert subprocess.run(
        ["git", "cat-file", "-e", staged_plan_blob],
        cwd=snapshot.snapshot_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).returncode != 0
    assert controller.workspace_plan_path is None
    assert config_after_switch.coder_thread_id == "revision-thread-1"
    assert config_after_switch.revision_coder_active is True
    assert config_after_switch.generation == 0
    assert config_after_switch.restart_count == 0
    assert store.get_health().restart_count == 0
    assert controller.completion_restarts == 0
    assert store.path(HANDOFF).read_text(encoding="utf-8") == ""
    assert controller.validations is original_validations
    assert controller._pending_adversary_report is pending_report
    assert store.get_bello_config().completion_return_count == first_completion_return_count

    assert controller.coder is not None
    controller.coder.mark_turn_completed("revision-turn-1")
    second_feedback = "Investigate and correct the confirmed seven-argument crash."
    second_decision = CompletionReviewDecision(
        decision="return",
        reason="adversary confirmed a crash",
        uncovered_behaviors=["seven-argument invocation must not crash"],
        message_to_coder=second_feedback,
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=5,
        generation=0,
    )

    second_source = (
        "adversary_report_controller"
        if first_source == "completion_review"
        else "completion_review"
    )
    await controller._return_completion_to_coder(second_decision, source=second_source)  # type: ignore[arg-type]

    assert len(client.thread_starts) == 1
    assert len(client.turn_starts) == 2
    assert client.turn_starts[1]["threadId"] == "revision-thread-1"
    assert client.turn_starts[1]["input"][0]["text"] == second_feedback
    assert client.turn_steers == []
    assert store.get_bello_config().completion_return_count == 1
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    switches = [event for event in events if event["event_type"] == "coder/profile_switch"]
    assert len(switches) == 1
    assert switches[0]["payload"]["previous_thread_id"] == "initial-thread"
    assert switches[0]["payload"]["revision_thread_id"] == "revision-thread-1"
    assert switches[0]["payload"]["source"] == first_source
    snapshot.cleanup()


async def test_revision_switch_waits_for_pending_initial_coder_delivery(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.old_turn_requested = asyncio.Event()
            self.release_old_turn = asyncio.Event()
            self.events = []

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "initial-thread":
                self.events.append("old-turn-requested")
                self.old_turn_requested.set()
                await self.release_old_turn.wait()
                self.events.append("old-turn-returned")
                return {"turn": {"id": "old-turn"}}
            self.events.append("revision-turn-started")
            return {"turn": {"id": "revision-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.events.append(f"interrupted:{thread_id}:{turn_id}")
            return {}

        async def thread_start(self, params, *, timeout):
            self.events.append("revision-thread-started")
            return {"thread": {"id": "revision-thread"}}

    client = RacingClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.workspace_root = tmp_path
    controller.workspace_task_path = task
    controller.store = store
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._coder_activity_mutex = None
    controller._coder_quiesce_mutex = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_snapshot = None
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="review found an edge case",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the reviewed edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Runtime feedback."))
    await client.old_turn_requested.wait()
    switch_task = asyncio.create_task(controller._return_completion_to_coder(decision))
    await asyncio.sleep(0)

    assert "revision-thread-started" not in client.events

    client.release_old_turn.set()
    delivered, turn_id = await delivery_task
    await switch_task

    assert delivered is True
    assert turn_id == "old-turn"
    assert client.events.index("old-turn-returned") < client.events.index(
        "interrupted:initial-thread:old-turn"
    )
    assert client.events.index("interrupted:initial-thread:old-turn") < client.events.index(
        "revision-thread-started"
    )
    runtime_config = store.get_bello_config()
    assert runtime_config.revision_coder_active is True
    assert runtime_config.coder_thread_id == "revision-thread"
    assert runtime_config.active_coder_turn_id == "revision-turn"


def test_selected_model_availability_includes_revision_coder_role() -> None:
    result = _selected_model_availability(
        {"data": [{"id": MODEL_GPT_5_6_SOL}]},
        coder_model=MODEL_GPT_5_6_SOL,
        runtime_model=MODEL_GPT_5_6_SOL,
        completion_model=MODEL_GPT_5_6_SOL,
        revision_coder_model=MODEL_GPT_5_6_LUNA,
    )

    assert result.missing_roles == (f"revision-coder={MODEL_GPT_5_6_LUNA}",)


@pytest.mark.parametrize("pause_stage", ["thread", "turn"])
async def test_revision_switch_cannot_overwrite_a_concurrent_pause(
    tmp_path: Path,
    pause_stage: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )
    plan = tmp_path / "PLAN.md"
    plan.write_text("PRIVATE INITIAL PLAN\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, task, plan_path=plan)

    class RacingClient:
        def __init__(self) -> None:
            self.request_started = asyncio.Event()
            self.allow_response = asyncio.Event()
            self.turn_starts = 0
            self.unsubscribed = []
            self.interrupted = []

        async def thread_start(self, params, *, timeout):
            if pause_stage == "thread":
                self.request_started.set()
                await self.allow_response.wait()
            return {"thread": {"id": "revision-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts += 1
            if pause_stage == "turn":
                self.request_started.set()
                await self.allow_response.wait()
            return {"turn": {"id": "revision-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.interrupted.append((thread_id, turn_id))
            return {}

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = RacingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        snapshot.snapshot_root,
        snapshot.task_path,
        thread_id="initial-thread",
        plan_path=snapshot.plan_path,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.plan_path = plan
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._revision_switch_in_progress = False
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_snapshot = snapshot
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="one defect remains",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the remaining edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    return_task = asyncio.create_task(controller._return_completion_to_coder(decision))
    controller._supervisor_task = return_task
    await client.request_started.wait()
    pause_task = asyncio.create_task(controller.pause())
    await asyncio.sleep(0)
    assert controller.paused is True
    assert return_task.cancelled() is False
    client.allow_response.set()
    await pause_task
    await asyncio.gather(return_task, return_exceptions=True)

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.active_coder_turn_id is None
    assert runtime_config.generation == 0
    assert runtime_config.restart_count == 0
    if pause_stage == "thread":
        assert controller.coder is initial_coder
        assert runtime_config.coder_thread_id == "initial-thread"
        assert runtime_config.revision_coder_active is False
        assert snapshot.plan_exposed is True
        assert snapshot.plan_path is not None and snapshot.plan_path.exists()
        assert controller.workspace_plan_path == snapshot.plan_path
        assert client.turn_starts == 0
        assert client.unsubscribed == ["revision-thread"]
        assert client.interrupted == []
    else:
        assert controller.coder is not initial_coder
        assert runtime_config.coder_thread_id == "revision-thread"
        assert runtime_config.revision_coder_active is True
        assert snapshot.plan_exposed is False
        assert snapshot.plan_path is not None and not snapshot.plan_path.exists()
        assert controller.workspace_plan_path is None
        assert client.turn_starts == 1
        assert client.unsubscribed == []
        assert client.interrupted == [("revision-thread", "revision-turn")]
    assert "revision_coder_switch_cancelled" in store.path(LOG).read_text(encoding="utf-8")
    snapshot.cleanup()


async def test_concurrent_coder_quiesce_waits_for_the_inflight_cleanup(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class BlockingCoder:
        def __init__(self) -> None:
            self.thread_id = "thread"
            self.active_turn_id = "turn"
            self.calls = 0
            self.first_call_started = asyncio.Event()
            self.release_first_call = asyncio.Event()

        async def interrupt(self) -> None:
            self.calls += 1
            if self.calls == 1:
                self.first_call_started.set()
                await self.release_first_call.wait()

    coder = BlockingCoder()
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.coder = coder
    controller._subagents = {}
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    first = asyncio.create_task(controller._quiesce_coder_tree("first"))
    await coder.first_call_started.wait()
    second = asyncio.create_task(controller._quiesce_coder_tree("second"))
    await asyncio.sleep(0)

    assert second.done() is False
    assert coder.calls == 1

    coder.release_first_call.set()
    assert await first is True
    assert await second is True
    assert coder.calls == 2


async def test_failed_stale_revision_interrupt_keeps_turn_id_for_lifecycle_retry(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task)),
        overwrite=True,
    )

    class FailingInterruptClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            raise AppServerError("interrupt failed")

    coder = CoderSession(
        FailingInterruptClient(),  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="revision-thread",
        active_turn_id="revision-turn",
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    with pytest.raises(AppServerError, match="interrupt failed"):
        await controller._interrupt_stale_revision_turn(
            coder,
            reason="concurrent pause",
        )

    assert coder.active_turn_id == "revision-turn"
    assert "stale_revision_turn" in store.path(LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure_stage", ["prepare", "thread", "turn"])
async def test_revision_coder_start_failure_finalizes_as_provider_failure(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            active_coder_turn_id="initial-turn" if failure_stage == "prepare" else None,
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )

    class FailingClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            if failure_stage == "prepare":
                raise AppServerError("initial coder interrupt unavailable")
            return {}

        async def thread_start(self, params, *, timeout):
            if failure_stage == "thread":
                raise AppServerError("thread/start unavailable")
            return {"thread": {"id": "revision-thread"}}

        async def turn_start(self, params, *, timeout):
            raise AppServerError("turn/start unavailable")

    client = FailingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
        active_turn_id="initial-turn" if failure_stage == "prepare" else None,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.workspace_root = tmp_path
    controller.workspace_task_path = task
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_snapshot = None
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    finalized = []

    async def record_finalize(
        result: str,
        *,
        status: BelloStatus,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))
        controller.running = False
        store.update_bello_config(lambda current: current.model_copy(update={"status": status}))

    controller.finalize = record_finalize  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="one defect remains",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the remaining edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller._return_completion_to_coder(decision)

    assert len(finalized) == 1
    assert finalized[0][1] == BelloStatus.PROVIDER_FAILURE
    expected_stage = "prepare" if failure_stage == "prepare" else f"{failure_stage}/start"
    assert f"{expected_stage} failed" in finalized[0][0]
    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PROVIDER_FAILURE
    assert runtime_config.completion_return_count == 1
    assert "revision_coder_switch_failed" in store.path(LOG).read_text(encoding="utf-8")
    if failure_stage in {"prepare", "thread"}:
        assert controller.coder is initial_coder
        assert runtime_config.coder_thread_id == "initial-thread"
        assert runtime_config.revision_coder_active is False
    else:
        assert controller.coder is not initial_coder
        assert runtime_config.coder_thread_id == "revision-thread"
        assert runtime_config.revision_coder_active is True
        assert runtime_config.active_coder_turn_id is None


async def test_late_root_turn_started_is_interrupted_without_resurrecting_paused_state(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class LateTurnClient:
        def __init__(self) -> None:
            self.interrupted = []

        async def turn_interrupt(self, thread_id, turn_id):
            self.interrupted.append((thread_id, turn_id))
            return {}

    client = LateTurnClient()
    coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        controller.task_path,
        thread_id="thread",
    )
    controller.client = client
    controller.coder = coder
    controller.paused = True
    controller._generation_has_coder_turn = False
    store.update_bello_config(
        lambda current: current.model_copy(
            update={"status": BelloStatus.PAUSED, "active_coder_turn_id": None}
        )
    )

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "turn/started",
                "params": {"threadId": "thread", "turnId": "late-turn"},
            }
        )
    )

    assert client.interrupted == [("thread", "late-turn")]
    assert coder.active_turn_id is None
    assert store.get_bello_config().active_coder_turn_id is None
    assert controller._generation_has_coder_turn is False
    assert "late_coder_turn_rejected" in store.path(LOG).read_text(encoding="utf-8")


async def test_stale_revision_interrupt_clears_matching_persisted_turn(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="revision-thread",
            active_coder_turn_id="revision-turn",
        ),
        overwrite=True,
    )

    class InterruptClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            return {}

    coder = CoderSession(
        InterruptClient(),  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="revision-thread",
        active_turn_id="revision-turn",
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    await controller._interrupt_stale_revision_turn(coder, reason="concurrent pause")

    assert coder.active_turn_id is None
    assert store.get_bello_config().active_coder_turn_id is None


async def test_restart_waits_for_pending_coder_turn_start_then_interrupts_old_turn(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.old_turn_requested = asyncio.Event()
            self.release_old_turn = asyncio.Event()
            self.events = []

        async def turn_start(self, params, *, timeout):
            thread_id = params["threadId"]
            if thread_id == "initial-thread":
                self.events.append("old-turn-requested")
                self.old_turn_requested.set()
                await self.release_old_turn.wait()
                self.events.append("old-turn-returned")
                return {"turn": {"id": "old-turn"}}
            self.events.append("restart-turn-started")
            return {"turn": {"id": "restart-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.events.append(f"interrupted:{thread_id}:{turn_id}")
            return {}

        async def thread_start(self, params, *, timeout):
            self.events.append("restart-thread-started")
            return {"thread": {"id": "restart-thread"}}

    client = RacingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Apply runtime feedback."))
    await client.old_turn_requested.wait()
    restart_task = asyncio.create_task(controller.restart("user requested restart"))
    await asyncio.sleep(0)

    assert restart_task.done() is False
    assert store.get_bello_config().status == BelloStatus.RESTARTING
    assert store.get_bello_config().generation == 0

    client.release_old_turn.set()
    delivered, turn_id = await delivery_task
    await restart_task

    assert delivered is False
    assert turn_id == "old-turn"
    assert client.events.index("old-turn-returned") < client.events.index(
        "interrupted:initial-thread:old-turn"
    )
    assert client.events.index("interrupted:initial-thread:old-turn") < client.events.index(
        "restart-thread-started"
    )
    runtime_config = store.get_bello_config()
    assert runtime_config.generation == 1
    assert runtime_config.coder_thread_id == "restart-thread"
    assert runtime_config.active_coder_turn_id == "restart-turn"
    assert runtime_config.status == BelloStatus.RUNNING


async def test_pause_supersedes_restart_during_new_thread_start(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.thread_start_requested = asyncio.Event()
            self.release_thread_start = asyncio.Event()
            self.turn_starts = 0

        async def thread_start(self, params, *, timeout):
            self.thread_start_requested.set()
            await self.release_thread_start.wait()
            return {"thread": {"id": "restart-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts += 1
            return {"turn": {"id": "restart-turn"}}

    client = RacingClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    restart_task = asyncio.create_task(controller.restart("runtime restart"))
    await client.thread_start_requested.wait()
    pause_task = asyncio.create_task(controller.pause())
    await asyncio.sleep(0)

    assert controller.paused is True
    assert store.get_bello_config().status == BelloStatus.PAUSED
    assert pause_task.done() is False

    client.release_thread_start.set()
    await restart_task
    await pause_task

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.coder_thread_id == "restart-thread"
    assert runtime_config.active_coder_turn_id is None
    assert client.turn_starts == 0


async def test_pause_does_not_cancel_an_inflight_terminal_finalize(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class RacingClient:
        def __init__(self) -> None:
            self.turn_start_requested = asyncio.Event()
            self.release_turn_start = asyncio.Event()
            self.interrupted = []
            self.stopped = False

        async def turn_start(self, params, *, timeout):
            self.turn_start_requested.set()
            await self.release_turn_start.wait()
            return {"turn": {"id": "pending-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.interrupted.append((thread_id, turn_id))
            return {}

        async def stop(self):
            self.stopped = True

    client = RacingClient()
    store.update_bello_config(
        lambda current: current.model_copy(update={"status": BelloStatus.RUNNING})
    )
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        controller.task_path,
        thread_id="thread",
    )
    controller._finalizing = False
    controller._coder_activity_mutex = None
    controller._coder_quiesce_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._subagents = {}

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Pending feedback."))
    await client.turn_start_requested.wait()
    finalize_task = asyncio.create_task(
        controller.finalize("terminal completion", status=BelloStatus.COMPLETE)
    )
    await asyncio.sleep(0)
    assert controller._finalizing is True

    await controller.pause()
    assert controller.paused is False
    assert finalize_task.done() is False

    client.release_turn_start.set()
    delivered, _ = await delivery_task
    await finalize_task

    assert delivered is False
    assert client.interrupted == [("thread", "pending-turn")]
    assert client.stopped is True
    assert store.get_bello_config().status == BelloStatus.COMPLETE


async def test_completion_accept_finalizes_without_deterministic_gate(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    decision = CompletionReviewDecision(
        decision="accept",
        reason="fresh validation passed",
        message_to_coder=None,
        persistent_decision=None,
        progress_update="Accepted by completion review.",
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller.apply_completion_decision(
        decision,
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert len(controller.completion_returns) == 0
    assert coder.messages == []
    assert "completion_accept_gate" not in store.path(LOG).read_text(encoding="utf-8")


async def test_completion_accept_still_checks_task_integrity_without_snapshot(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller._canonical_task_hash = _hash_file(task)
    packet = _gate_packet(task, validations=validations)
    task.write_text("# Changed task", encoding="utf-8")

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.ESCALATED
    assert controller.completion_returns == []
    assert coder.messages == []
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted workspace failed task integrity validation" in report
    assert "the original task file changed after the run started" in report


async def test_completion_accept_does_not_require_independent_changed_test_evidence(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app_new.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app_new.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_files = [
        ChangedFile(path="src/app.py", status="M", sequence=2),
        ChangedFile(path="tests/test_app_new.py", status="A", sequence=2),
    ]
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app_new.py",
            file_kind="test",
            change_kind="added",
            diff="+def test_requested_behavior():\n+    assert app() == 'requested'",
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert len(controller.completion_returns) == 0
    assert coder.messages == []


async def test_adversary_remaining_limit_runs_before_completion_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = None
    controller.client = object()
    controller.model = None
    controller.running = False
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    (tmp_path / ".supervisor" / "secret.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".supervisor" / "secret.txt").write_text("runtime history", encoding="utf-8")
    seen_snapshot_roots: list[Path] = []

    class FakeAdversary:
        def __init__(self, client, project_root, *, on_thread_start=None, on_thread_done=None, **kwargs) -> None:
            self.project_root = Path(project_root)
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            seen_snapshot_roots.append(self.project_root)
            assert self.project_root != tmp_path
            assert (self.project_root / "TASK.md").exists()
            assert not (self.project_root / ".supervisor").exists()
            (self.project_root / "adversary_probe.txt").write_text("probe", encoding="utf-8")
            assert previous_adversary_report is None
            if self.on_thread_start:
                self.on_thread_start("adv-thread")
            if self.on_thread_done:
                self.on_thread_done("adv-thread")
            return SimpleNamespace(
                report_text=(
                    "attacked: boundary inputs\n"
                    "findings: none\n"
                    "held: boundary inputs held\n"
                    "not_reached: none\n"
                    "overall: held"
                ),
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller._pending_adversary_report is not None
    assert controller._pending_adversary_report.thread_id == "adv-thread"
    assert controller._pending_adversary_report.candidate_finding is False
    assert controller._pending_adversary_report.latest_relevant_change_sequence == 2
    assert controller._pending_adversary_report.workspace_state_id is not None
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == []
    assert seen_snapshot_roots and not seen_snapshot_roots[0].exists()
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversarial tester completed" in progress
    assert "adv_report_controller found no findings or observations" in progress


async def test_adversary_run_limit_skips_additional_run_and_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 1, "adversary_run_count": 1})
    )

    class UnexpectedAdversary:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("adversary should not run after limit is reached")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", UnexpectedAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Skipping adversarial tester before complete: adversary run limit reached (1/1)" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "adversary/limit_reached" for event in events)


async def test_adversary_infra_failure_completes_with_recorded_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An adversary that cannot run is a tester-availability problem, not evidence against
    # the accepted work: the run must finalize the accept with the gap recorded loudly,
    # not die as infrastructure-invalid.
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.running = False
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None

    class FailingAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            raise AdversaryAgentError("adversary did not produce an agent message")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FailingAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert coder.messages == []
    accepted = controller._accepted_adversary_report
    assert accepted is not None
    assert accepted.status == "error"
    assert "did not produce an agent message" in accepted.report_text
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversarial tester could not run" in progress
    assert "adversary coverage recorded as missing" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "adversary/unavailable" for event in events)
    assert any(event["event_type"] == "completion/accept" for event in events)
    final_report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "status=error" in final_report
    assert "provider_failure" not in final_report.lower()


async def test_adversary_fresh_report_allows_completion_finalize(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    packet = _gate_packet(task, validations=validations)
    packet.adversary_report = AdversaryReport(
        report_text="attacked: boundary\nfindings: none\noverall: held",
        thread_id="adv-thread",
        turn_id="adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=2,
        validation_sequence=3,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert coder.messages == []
    final_report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "## Adversary Reports" in final_report


async def test_adversary_report_controller_routes_schema_valid_normalized_report_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    fake = _RuntimeFakeSupervisor(store, task)
    controller.supervisor = fake
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.coder_model = MODEL_GPT_5_5
    controller.supervisor_model = MODEL_GPT_5_5
    controller.adversary_model = "gpt-adversary"
    controller.adversary_intelligence = "ultra"
    controller.running = True
    controller.observed_changed_files = {"src/app.py": ChangedFile(path="src/app.py", status="M", sequence=2)}
    normalized = _FakeAdvReportController(
        [
            AdvReportControllerDecision(
                forward_to_coder=True,
                reason="kept one reworded finding",
                report_to_coder=(
                    "## Findings requiring correction\n"
                    "- invoking with seven positional arguments crashes"
                ),
            )
        ]
    )
    controller.adv_report_controller = normalized

    class FakeAdversary:
        def __init__(
            self,
            *args,
            model=None,
            intelligence=None,
            on_thread_start=None,
            on_thread_done=None,
            **kwargs,
        ) -> None:
            assert model == "gpt-adversary"
            assert intelligence == "ultra"
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            assert previous_adversary_report is None
            if self.on_thread_start:
                self.on_thread_start("adv-thread")
            if self.on_thread_done:
                self.on_thread_done("adv-thread")
            return SimpleNamespace(
                report_text="attacked: stack args\nfindings: crash on seven args\noverall: broke",
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=True,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert fake.completion_packets == []
    assert len(normalized.packets) == 1
    assert normalized.packets[0].adversary_report is not None
    assert normalized.packets[0].adversary_report.candidate_finding is True
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == [
        "Finding: a confirmed defect that requires correction.\n"
        "Observation: a concern that is not yet confirmed; investigate it and fix it only if confirmed.\n\n"
        "## Findings requiring correction\n"
        "- invoking with seven positional arguments crashes"
    ]
    assert "attacked:" not in coder.messages[0]
    assert "overall:" not in coder.messages[0]
    assert controller.completion_returns[0].source == "adversary_report_controller"
    assert store.get_bello_config().completion_return_count == 0
    coder_readable_log = store.path(LOG).read_text(encoding="utf-8")
    assert "attacked: stack args" not in coder_readable_log
    assert "overall: broke" not in coder_readable_log


async def test_adversary_observations_are_routed_when_candidate_finding_is_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(
        tmp_path,
        validations=validations,
    )
    controller.adversary_enabled = True
    controller.client = object()
    controller.running = True
    controller.adv_report_controller = _FakeAdvReportController(
        [
            AdvReportControllerDecision(
                forward_to_coder=True,
                reason="carried one observation",
                report_to_coder=(
                    "## Observations requiring investigation\n"
                    "- cache count changed without the expected header"
                ),
            )
        ]
    )

    class ObservingAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            return SimpleNamespace(
                report_text=(
                    "candidate_finding: false\n"
                    "attacked: cache behavior\n"
                    "findings: none\n"
                    "observations:\n"
                    "- cache count changed without the expected header\n"
                    "held: ordinary cache path\n"
                    "overall: I believe no defects remain in the submitted solution"
                ),
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", ObservingAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.STARTING
    assert len(coder.messages) == 1
    assert "## Observations requiring investigation" in coder.messages[0]
    assert "cache count changed without the expected header" in coder.messages[0]
    assert "attacked:" not in coder.messages[0]


async def test_completion_return_budget_waits_for_coder_readiness_before_forcing_adversary(
    tmp_path: Path,
) -> None:
    controller, store, _, coder = _completion_gate_controller(tmp_path, validations=[])
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 1,
                "max_completion_returns_after_adversary": 2,
            }
        )
    )
    decision = CompletionReviewDecision(
        decision="return",
        reason="one material gap remains",
        validation_gaps=["edge case is not validated"],
        message_to_coder="Fix and validate the edge case, then report readiness again.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller._return_completion_to_coder(decision)

    cfg = store.get_bello_config()
    assert cfg.completion_return_count == 1
    assert cfg.completion_returns_since_adversary == 0
    assert cfg.adversary_run_count == 0
    assert coder.messages == ["Fix and validate the edge case, then report readiness again."]
    assert controller._completion_review_budget_action() == "adversary"


async def test_completion_only_review_budget_finalizes_without_restart_or_extra_review(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 4,
                "completion_return_count": 4,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    await controller._run_supervisor_check("coder ready after final allowed review", None, None, None, None, True)

    assert fake.completion_packets == []
    assert controller.completion_restarts == 0
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]


def test_completion_only_zero_review_budget_skips_review(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_completion_returns_before_adversary": 0,
                "completion_return_count": 100,
            }
        )
    )

    assert controller._completion_review_budget_action() == "complete"


def test_completion_only_unlimited_review_budget_has_no_cap(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_completion_returns_before_adversary": "unlimited",
                "completion_return_count": 100,
            }
        )
    )

    assert controller._completion_review_budget_action() is None


def test_zero_pre_adversary_review_budget_starts_adversary(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 0,
                "completion_return_count": 0,
            }
        )
    )

    assert controller._completion_review_budget_action() == "adversary"


def test_zero_post_adversary_review_budget_completes(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_after_adversary": 0,
                "adversary_run_count": 1,
                "completion_returns_since_adversary": 0,
            }
        )
    )

    assert controller._completion_review_budget_action() == "complete"


def test_zero_post_adversary_budget_has_no_legacy_completion_adjudication(tmp_path: Path) -> None:
    controller, store, task, _ = _completion_gate_controller(tmp_path, validations=[])
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_after_adversary": 0,
                "adversary_run_count": 1,
                "completion_returns_since_adversary": 0,
            }
        )
    )
    packet = _gate_packet(task, validations=[])
    packet.adversary_report = AdversaryReport(
        candidate_finding=True,
        report_text="attacked: edge\nfindings: candidate defect\noverall: broke",
        generation=packet.generation,
        completion_wake_sequence=packet.wake_sequence,
        latest_relevant_change_sequence=packet.latest_relevant_change_sequence,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    assert controller._completion_review_budget_action(packet=packet) == "complete"


async def test_pre_adversary_return_budget_runs_adversary_without_an_extra_completion_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.adversary_model = "gpt-adversary"
    controller.adversary_intelligence = "ultra"
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 7,
                "max_completion_returns_after_adversary": 2,
                "completion_return_count": 7,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    class CleanAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            return SimpleNamespace(
                report_text="attacked: boundaries\nfindings: none\noverall: held",
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", CleanAdversary)

    await controller._run_supervisor_check("coder ready", None, None, None, None, True)

    cfg = store.get_bello_config()
    assert fake.completion_packets == []
    assert cfg.adversary_run_count == 1
    assert cfg.completion_returns_since_adversary == 0
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]


async def test_post_adversary_return_budget_finalizes_on_next_readiness_without_extra_review(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 7,
                "max_completion_returns_after_adversary": 2,
                "adversary_run_count": 1,
                "completion_return_count": 9,
                "completion_returns_since_adversary": 2,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    await controller._run_supervisor_check("coder ready after final return", None, None, None, None, True)

    assert fake.completion_packets == []
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "completion/budget_finalize"
    assert events[-1]["decision"]["completion_return_count"] == 9
    assert events[-1]["reason"] == "post-adversary completion review budget exhausted"


async def test_required_budget_adversary_failure_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.client = object()
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 1,
                "completion_return_count": 1,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    class FailingAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            raise AdversaryAgentError("provider unavailable")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FailingAdversary)

    await controller._run_supervisor_check("coder ready", None, None, None, None, True)

    assert fake.completion_packets == []
    assert finalized == [
        (
            "required adversary failed under bounded review policy: provider unavailable",
            BelloStatus.PROVIDER_FAILURE,
            False,
        )
    ]
    assert controller._pending_adversary_report.status == "error"


async def test_adversary_receives_previous_report_as_regression_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.running = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 2, "adversary_run_count": 1})
    )
    controller._pending_adversary_report = AdversaryReport(
        candidate_finding=True,
        report_text="attacked: previous edge\nfindings: previous crash\noverall: broke",
        thread_id="old-adv-thread",
        turn_id="old-adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=1,
        validation_sequence=2,
        workspace_state_id="old-state",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    class FakeAdversary:
        def __init__(self, *args, on_thread_start=None, on_thread_done=None, **kwargs) -> None:
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            assert previous_adversary_report is not None
            assert previous_adversary_report["report_text"].startswith("attacked: previous edge")
            if self.on_thread_start:
                self.on_thread_start("new-adv-thread")
            if self.on_thread_done:
                self.on_thread_done("new-adv-thread")
            return SimpleNamespace(
                report_text="attacked: previous edge, fresh edge\nfindings: none\noverall: held",
                thread_id="new-adv-thread",
                turn_id="new-adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller._accepted_adversary_report.thread_id == "new-adv-thread"
    assert store.get_bello_config().adversary_run_count == 2
    assert coder.messages == []


async def test_completion_return_never_appends_raw_adversary_report(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.adversary_report = AdversaryReport(
        report_text="attacked: stack args\nfindings: crash on seven args\nraw observed output: SIGSEGV\noverall: broke",
        thread_id="adv-thread",
        turn_id="adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=2,
        validation_sequence=3,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = CompletionReviewDecision(
        decision="return",
        reason="adversary reproduced stack arg crash",
        uncovered_behaviors=["stack-passed arguments crash"],
        validation_gaps=[],
        claim_evidence_mismatches=[],
        packet_or_access_limitations=[],
        changed_test_risks=[],
        message_to_coder="Fix the reproduced stack-argument crash.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert len(controller.completion_returns) == 1
    assert coder.messages == ["Fix the reproduced stack-argument crash."]
    assert "Adversarial tester report:" not in coder.messages[0]
    assert "SIGSEGV" not in coder.messages[0]


async def test_completion_accept_is_not_overridden_by_changed_test_masking_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app.py",
            file_kind="test",
            change_kind="modified",
            diff=(
                "diff --git a/tests/test_app.py b/tests/test_app.py\n"
                "@@\n"
                "-    assert app() == 'requested'\n"
                "+    assert True\n"
            ),
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_accept_is_not_overridden_by_skipped_test_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app.py",
            file_kind="test",
            change_kind="modified",
            diff="+test.skip('requested behavior', () => expect(app()).toBe('requested'))",
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_accept_is_not_overridden_by_behavioral_validation_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="python -m py_compile src/app.py",
            exit_code=0,
            type="static",
            passed=True,
            summary="compiled",
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_return_is_not_blocked_by_controller_freshness_gate(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            validation_id="validation-new",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario fixed",
            exit_code=0,
            type="behavior_demo",
            passed=True,
            trusted_validation_outcome="passed",
            summary="fixed=1",
            captured_output="fixed=1\n",
            sequence=12,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations, wake_sequence=20, latest_change=11)
    packet.latest_event_sequence = 20
    decision = CompletionReviewDecision.model_validate(
        {
            "decision": "return",
            "reason": "old gap still open",
            "decision_artifact": {
                "current_state": "old state",
                "resolved_concerns": [],
                "stale_concerns": ["old gap"],
                "uncovered_edge_candidates": [],
                "actionable_gap_or_none": "old gap",
            },
            "files_reviewed": [
                {"path": "src/app.py", "reason": "changed source", "kind": "source", "inspected": True, "limitation": None}
            ],
            "behavior_evidence_matrix": [
                {
                    "behavior": "requested behavior",
                    "task_basis": "TASK.md",
                    "files_considered": ["src/app.py"],
                    "evidence": [],
                    "status": "partial",
                    "gap": "old gap",
                }
            ],
            "uncovered_behaviors": ["requested behavior"],
            "validation_gaps": [],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "fix old gap",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 20,
            "generation": 0,
        }
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert store.get_bello_config().status == BelloStatus.STARTING
    assert len(controller.completion_returns) == 1
    assert coder.messages == ["fix old gap"]
    assert "completion_decision_staleness_failure" not in store.path(LOG).read_text(encoding="utf-8")


async def test_completion_restart_writes_handoff_and_starts_new_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.started_turns = []

        async def respond(self, request_id, response):
            return None

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "new-thread"}}

        async def turn_start(self, params, *, timeout):
            self.started_turns.append(params)
            return {"turn": {"id": "new-turn"}}

    handoff = RestartHandoff(
        objective="task",
        restart_reason="repeated completion miss",
        bad_pattern="validated only happy path",
        known_evidence="fallback unvalidated",
        next_step="read task",
        recovery_signal="fallback validated",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.model = None
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = None
    controller.pending_approvals = {}
    controller.prior_interventions = []
    controller.validations = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = [
        {
            "reason": "fallback missing",
            "uncovered_behaviors": ["fallback"],
            "validation_gaps": [],
            "message_to_coder": "cover fallback",
            "sequence": 1,
            "generation": 0,
        }
    ]
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="restart",
            reason="non-converging completion returns",
            uncovered_behaviors=["fallback"],
            validation_gaps=["same stale validation"],
            message_to_coder=None,
            persistent_decision=None,
            progress_update="Restarting from completion review.",
            clear_handoff=False,
            display_message=None,
            handoff=handoff,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    assert store.get_bello_config().generation == 1
    assert "repeated completion miss" in store.path(HANDOFF).read_text(encoding="utf-8")
    assert controller.completion_restarts == 1
    assert controller.client.started_turns


async def test_transport_error_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller._sequence = 0

    await controller.handle_controller_event(
        ControllerEvent(
            kind="transport_error",
            error_message="app-server stdout line exceeded stream limit (64 bytes): test payload",
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server transport error" in text
    assert controller.running is False


async def test_interrupted_coder_turn_resumes_same_thread_with_continuation(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="coder-thread",
            active_coder_turn_id="old-turn",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class RecoverableCoder:
        thread_id = "coder-thread"
        active_turn_id = "old-turn"

        def __init__(self) -> None:
            self.messages: list[str] = []

        async def resume_thread(self):
            return {
                "id": "coder-thread",
                "turns": [
                    {"id": "old-turn", "status": "interrupted", "items": []}
                ],
            }

        async def start_turn(self, message: str):
            self.messages.append(message)
            self.active_turn_id = "recovery-turn"
            store.update_bello_config(
                lambda cfg: cfg.model_copy(
                    update={"active_coder_turn_id": "recovery-turn"}
                )
            )
            return "recovery-turn"

    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.coder = RecoverableCoder()

    await controller._recover_coder_thread_after_transport(start_continuation=True)

    assert controller.coder.thread_id == "coder-thread"
    assert controller.coder.active_turn_id == "recovery-turn"
    assert len(controller.coder.messages) == 1
    assert "current workspace state" in controller.coder.messages[0]
    assert store.get_bello_config().active_coder_turn_id == "recovery-turn"


async def test_supervisor_turn_start_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class HangingTurnStartClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            await asyncio.Event().wait()

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller.supervisor = StatelessSupervisorAgent(
        HangingTurnStartClient(),
        store,
        task,
        timeout_seconds=0.01,
    )  # type: ignore[arg-type]

    await controller._run_supervisor_check("check latest state", None, None, None, None)

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "supervisor check failed" in text
    assert "supervisor turn/start response timed out after 0.01s" in text
    assert "thread_id=supervisor-thread" in text
    assert controller.running is False
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["status"] == "error"
    assert audit["thread_id"] == "supervisor-thread"
    assert audit["turn_id"] is None
    assert "supervisor turn/start response timed out after 0.01s" in audit["error"]


async def test_stale_runtime_supervisor_timeout_retries_before_queued_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class HangingTurnStartClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            await asyncio.Event().wait()

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller.supervisor = StatelessSupervisorAgent(
        HangingTurnStartClient(),
        store,
        task,
        timeout_seconds=0.01,
    )  # type: ignore[arg-type]
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._run_supervisor_check("stale runtime check", None, None, None, None)

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert text == ""
    assert controller.running is True
    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_completion_summary is not None
    assert "retrying the retained runtime trigger before completion" in store.path(PROGRESS).read_text(encoding="utf-8")
    assert any("supervisor check failed" in message for _, message in controller.tui.messages)


async def test_supervisor_no_message_retries_from_latest_stable_state(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class NoMessageThenNoopSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            if self.calls == 1:
                raise SupervisorAgentError("supervisor did not produce an agent message")
            return SupervisorDecision(
                decision=SupervisorDecisionKind.NOOP,
                reason="recovered",
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    supervisor = NoMessageThenNoopSupervisor(store, controller.task_path)
    controller.supervisor = supervisor

    await controller._supervisor_check_loop("runtime check", None, None, None, None, False)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    # After a successful recovery the consecutive no_message budget resets, so a recovered
    # provider does not carry earlier blips toward infra-invalid.
    assert controller.provider_failure_recovery_counts == {}
    assert "supervisor produced no agent message" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_repeated_runtime_supervisor_no_message_skips_current_review(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageRuntimeSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

    supervisor = AlwaysNoMessageRuntimeSupervisor(store, controller.task_path)
    controller.supervisor = supervisor

    await controller._supervisor_check_loop("runtime check", None, None, None, None, False)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert controller.running is True
    assert controller._supervisor_dirty is False
    assert controller.provider_failure_recovery_counts["no_message"] == 2
    assert controller.provider_failure_recovery_counts["runtime_monitor_no_message"] == 2
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "retrying review from latest stable state" in progress
    assert "skipping this runtime-only review" in progress


async def test_runtime_no_message_exhaustion_explicitly_skips_and_acks_pending_large_diff(
    tmp_path: Path,
) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageRuntimeSupervisor:
        def __init__(self, state_store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, state_store, task)  # type: ignore[arg-type]

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            raise SupervisorAgentError("supervisor did not produce an agent message")

    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed",
        ),
        validation=None,
        changed_files=changed_files,
    )
    pending_signature = controller._runtime_pending_trigger_signatures()["large_diff"][0]
    controller.supervisor = AlwaysNoMessageRuntimeSupervisor(store, controller.task_path)

    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): file change completed",
        None,
        None,
        None,
        None,
        False,
    )

    assert decision.reasons == ("large_diff",)
    assert controller._last_large_diff_signature == pending_signature
    assert "large_diff" not in controller._runtime_pending_trigger_signatures()
    assert "skipping this runtime-only review" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_repeated_supervisor_no_message_marks_infra_invalid_provider_failure(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide_completion(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

        async def close_completion_review(self):
            return None

    supervisor = AlwaysNoMessageSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    # Pin the configurable completion no_message budget low and disable backoff so the test
    # reaches the infra-invalid path fast (default budget rides out a transient blip).
    controller._completion_no_message_max_retries = 1
    controller._no_message_backoff_seconds = ()

    await controller._supervisor_check_loop("completion check", None, None, None, None, True)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "infra-invalid: supervisor no_message provider failure after retry/resume" in report
    assert "- Status: provider_failure" in report
    assert "repeated supervisor no_message" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_completion_no_message_budget_rides_out_blip_before_infra_invalid(tmp_path: Path) -> None:
    # A transient provider blip (empty completions) must be ridden out with backed-off retries
    # up to the configurable budget; infra-invalid only fires after the full budget is spent.
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide_completion(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

        async def close_completion_review(self):
            return None

    supervisor = AlwaysNoMessageSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    controller._completion_no_message_max_retries = 3
    controller._no_message_backoff_seconds = ()  # no real sleeping in the test

    await controller._supervisor_check_loop("completion check", None, None, None, None, True)

    # 3 retries then the infra-invalid attempt = 4 model calls (old behavior gave up after 1 retry).
    assert supervisor.calls == 4
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


async def test_preflight_appserver_timeout_writes_provider_failure_final_report(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class PreflightTimeoutClient:
        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            return None

        async def account_read(self):
            raise AppServerTimeoutError("app-server RPC account/read response timed out after 30s")

    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=PreflightTimeoutClient(),  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    text = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed" in text
    assert "account/read response timed out" in text


async def test_missing_selected_model_interrupts_before_coder_and_writes_final_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class MissingModelClient:
        def __init__(self) -> None:
            self.thread_started = False
            self.stopped = False

        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            self.stopped = True

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": MODEL_GPT_5_6_SOL}, {"id": MODEL_GPT_5_5}]}

        async def thread_start(self, params):
            self.thread_started = True
            raise AssertionError("coder must not start with an unavailable model")

    client = MissingModelClient()
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model="gpt-5.6-unknown",
        supervisor_model=MODEL_GPT_5_6_SOL,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in report
    assert "model availability preflight failed before coder start" in report
    assert "coder=gpt-5.6-unknown" in report
    assert "Available models: gpt-5.5, gpt-5.6-sol" in report
    assert ".supervisor/FINAL_REPORT.md" in report
    assert client.thread_started is False
    assert client.stopped is True


async def test_missing_fixed_adversary_model_interrupts_before_coder_and_writes_final_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class MissingAdversaryModelClient:
        def __init__(self) -> None:
            self.thread_started = False
            self.stopped = False

        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            self.stopped = True

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": MODEL_GPT_5_5}]}

        async def thread_start(self, params):
            self.thread_started = True
            raise AssertionError("coder must not start with an unavailable adversary model")

    client = MissingAdversaryModelClient()
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model=MODEL_GPT_5_5,
        supervisor_model=MODEL_GPT_5_5,
        overwrite_state=True,
        use_git_diff=False,
        completion_review=True,
        adversary_enabled=True,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in report
    assert "model availability preflight failed before coder start" in report
    assert f"adversary={ADVERSARY_MODEL}" in report
    assert "Available models: gpt-5.5" in report
    assert client.thread_started is False
    assert client.stopped is True


async def test_preflight_probe_cleanup_unsubscribes_and_logs_without_failing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class ProbeCleanupClient:
        def __init__(self) -> None:
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
                return {
                    "thread": {"id": "probe-thread"},
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
                }

        async def thread_archive(self, thread_id):
            raise AssertionError("preflight probe cleanup should not archive threads without rollouts")

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            raise AppServerError("unsubscribe cleanup failed")

    client = ProbeCleanupClient()
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    assert client.unsubscribed == ["probe-thread"]
    config = controller.store.get_bello_config()
    assert config.model == DEFAULT_MODEL
    assert config.coder_model == DEFAULT_MODEL
    assert config.supervisor_model == DEFAULT_MODEL
    log_lines = controller.store.path(LOG).read_text(encoding="utf-8").splitlines()
    assert log_lines
    entry = json.loads(log_lines[-1])
    assert entry["type"] == "cleanup_error"
    assert entry["cleanup_kind"] == "preflight_probe_thread"
    assert entry["thread_id"] == "probe-thread"
    assert entry["error_type"] == "AppServerError"


async def test_preflight_rate_limit_probe_failure_warns_and_continues(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class RateLimitFailureClient:
        def __init__(self) -> None:
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            raise AppServerError(
                "{'code': -32603, 'message': 'failed to fetch codex rate limits: error sending request'}"
            )

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
                return {
                    "thread": {"id": "probe-thread"},
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
                }

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = RateLimitFailureClient()
    tui = _FakeTUI()
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=tui,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    config = controller.store.get_bello_config()
    assert config.model == DEFAULT_MODEL
    assert config.coder_model == DEFAULT_MODEL
    assert config.supervisor_model == DEFAULT_MODEL
    assert client.unsubscribed == ["probe-thread"]
    assert any("rate limit check unavailable" in message for _, message in tui.messages)
    log_lines = controller.store.path(LOG).read_text(encoding="utf-8").splitlines()
    assert log_lines
    entry = json.loads(log_lines[-1])
    assert entry["type"] == "preflight_warning"
    assert entry["check"] == "codex_rate_limits"
    assert entry["error_type"] == "AppServerError"


async def test_preflight_accepts_configured_danger_full_access_sandbox(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class DangerSandboxClient:
        def __init__(self) -> None:
            self.thread_params: dict | None = None
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
            self.thread_params = params
            return {
                "thread": {"id": "probe-thread"},
                "approvalPolicy": "on-request",
                "sandbox": "danger-full-access",
            }

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = DangerSandboxClient()
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "danger-full-access")
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    assert client.thread_params is not None
    assert client.thread_params["sandbox"] == "danger-full-access"
    assert client.unsubscribed == ["probe-thread"]


async def test_server_request_respond_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class RespondTimeoutClient:
        async def respond(self, request_id, response):
            raise AppServerTimeoutError("app-server respond 61 send timed out after 15s")

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = RespondTimeoutClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = None
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.running = True

    await controller.handle_controller_event(
        ControllerEvent(
            kind="server_request",
            message=AppServerMessage(
                {
                    "id": 61,
                    "method": "item/fileChange/requestApproval",
                    "params": {"grantRoot": str(tmp_path / "src.py"), "availableDecisions": ["accept", "decline"]},
                }
            ),
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed while handling server_request" in text
    assert "respond 61 send timed out" in text
    assert controller.running is False


async def test_coder_turn_start_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="coder-thread"),
        overwrite=True,
    )

    class CoderTurnTimeoutClient:
        async def respond(self, request_id, response):
            return None

        async def turn_start(self, params, *, timeout):
            assert timeout == APP_SERVER_CODER_RPC_TIMEOUT_SECONDS
            raise AppServerTimeoutError(f"app-server RPC turn/start response timed out after {timeout:g}s")

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = CoderTurnTimeoutClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = CoderSession(
        controller.client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="coder-thread",
    )
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.running = True

    await controller.handle_controller_event(
        ControllerEvent(
            kind="server_request",
            message=AppServerMessage(
                {
                    "id": 62,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                        "availableDecisions": ["accept", "decline"],
                    },
                }
            ),
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed while handling server_request" in text
    assert "turn/start response timed out after 3600s" in text
    assert controller.running is False


@pytest.mark.parametrize(
    ("revision_active", "expected_model", "expected_intelligence"),
    [
        (False, "gpt-coder", "high"),
        (True, "gpt-revision", "medium"),
    ],
)
async def test_restart_preserves_active_coder_profile(
    tmp_path: Path,
    revision_active: bool,
    expected_model: str,
    expected_intelligence: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            revision_coder_enabled=True,
            revision_coder_mod="gpt-revision",
            revision_coder_intelligence="medium",
            revision_coder_active=revision_active,
        ),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turn_params = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": "restart-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params.append(params)
            return {"turn": {"id": "restart-turn", "status": "completed"}}

    client = FakeClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.approvals = None
    controller.coder = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller._sequence = 0
    controller.coder_model = "gpt-coder"
    controller.coder_intelligence = "high"
    controller.fast = False

    await controller.restart("test restart")

    assert controller.coder is not None
    assert controller.coder.model == expected_model
    assert controller.coder.intelligence == expected_intelligence
    assert client.thread_params[-1]["model"] == expected_model
    assert client.turn_params[-1]["effort"] == expected_intelligence
    assert store.get_bello_config().revision_coder_active is revision_active


async def test_user_restart_cancels_inflight_supervisor_task_before_root_swap(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="old-thread",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "new-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "new-turn"}}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = FakeClient()
    controller.coder = CoderSession(
        controller.client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="old-thread",
    )
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    cancelled = asyncio.Event()

    async def inflight_adversary_like_task() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor_task = asyncio.create_task(inflight_adversary_like_task())
    controller._supervisor_task = supervisor_task
    await asyncio.sleep(0)

    await controller.restart("user requested restart")

    assert cancelled.is_set()
    assert supervisor_task.cancelled()
    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.RUNNING
    assert runtime_config.generation == 1
    assert runtime_config.coder_thread_id == "new-thread"
    assert runtime_config.active_coder_turn_id == "new-turn"


async def test_restart_while_paused_requires_explicit_resume(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="paused-thread",
            status=BelloStatus.PAUSED,
        ),
        overwrite=True,
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.paused = True
    controller._finalizing = False
    controller.tui = _FakeTUI()

    await controller.restart("restart requested while paused")

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.generation == 0
    assert runtime_config.coder_thread_id == "paused-thread"
    assert controller.tui.messages[-1] == ("STATUS", "paused; resume before restarting")


async def test_supervisor_decision_can_clear_handoff(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    store.write_handoff("restart context\n")

    controller = BelloController.__new__(BelloController)
    controller.store = store

    await controller.apply_supervisor_decision(
        SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            clear_handoff=True,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id=None,
    )

    assert store.path(HANDOFF).read_text(encoding="utf-8") == ""


def test_structured_handoff_is_read_back_verbatim(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    handoff = RestartHandoff(
        objective="task",
        restart_reason="loop",
        bad_pattern="repeat",
        known_evidence="evidence",
        next_step="step",
        recovery_signal="signal",
    )
    store.write_handoff(handoff.model_dump_json(indent=2) + "\n")

    packet = StatelessSupervisorAgent(None, store, task).build_packet(  # type: ignore[arg-type]
        wake_sequence=1,
        current_summary="progress check",
    )

    assert packet.handoff == handoff


async def test_controller_approval_packet_carries_structured_context(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    context = normalize_approval_request(
        AppServerMessage(
            {
                "id": 42,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "t",
                    "turnId": "u",
                    "itemId": "i",
                    "command": "pytest",
                    "cwd": str(tmp_path),
                    "availableDecisions": ["accept", "decline"],
                },
            }
        )
    )

    class FakeSupervisor:
        def __init__(self) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.packet = None

        def build_packet(self, **kwargs):
            self.packet = self.agent.build_packet(**kwargs)
            return self.packet

        async def decide(self, packet):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.NOOP,
                reason="ok",
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    fake = FakeSupervisor()
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.project_root = tmp_path
    controller.task_path = task
    controller.supervisor = fake
    controller.pending_approvals = {context.server_request_id: context}
    controller.last_coder_message = CoderMessage(text="ready", sequence=1)
    controller.validations = [ValidationRun(command="pytest", exit_code=1, passed=False, summary="failed", sequence=2)]
    controller.prior_interventions = [PriorIntervention(reason="drift", message_to_coder="focus", sequence=3)]
    controller.use_git_diff = False

    await controller.decide_approval(context, "needs judgment")

    packet = fake.packet
    assert packet.approval_context.command == "pytest"
    assert packet.approval_context.available_decisions == ["accept", "decline"]
    assert len(packet.pending_approvals) == 1
    assert packet.last_coder_message.text == "ready"
    assert packet.validations[0].passed is False
    assert packet.prior_interventions[0].message_to_coder == "focus"


async def test_supervisor_deny_reason_is_steered_to_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )
    context = normalize_approval_request(
        AppServerMessage(
            {
                "id": 51,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "curl https://example.com", "availableDecisions": ["accept", "decline", "cancel"]},
            }
        )
    )

    class FakeSupervisor:
        async def decide_approval(self, context, reason):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.DENY,
                approval_decision="decline",
                reason="Network access is not required by the task.",
                message_to_coder="do not use this",
            )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path, supervisor=FakeSupervisor())
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(AppServerMessage({"id": 51, "method": context.server_request_method, "params": context.raw_params}))

    assert controller.client.responses == [(51, {"decision": "decline"})]
    assert controller.coder.messages == ["Network access is not required by the task."]


async def test_policy_deny_reason_is_steered_to_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 52,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    assert controller.client.responses == [(52, {"decision": "decline"})]
    assert controller.coder.messages == ["writes to supervisor runtime/state files are denied"]


async def test_adversary_file_change_request_is_denied_without_steering_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="coder-thread"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller._active_adversary_thread_id = "adv-thread"

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 53,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "threadId": "adv-thread",
                    "turnId": "adv-turn",
                    "grantRoot": str(tmp_path / "src" / "app.py"),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    assert controller.client.responses == [(53, {"decision": "decline"})]
    assert controller.coder.messages == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversary approval denied without steering coder" in progress
    # The denial is remembered so an adversary retry can be told what was refused.
    assert len(controller._adversary_denied_commands) == 1
    assert str(tmp_path / "src" / "app.py") in controller._adversary_denied_commands[0]
    assert "(denied:" in controller._adversary_denied_commands[0]


async def test_policy_deny_no_active_turn_starts_new_coder_turn(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.active_turn_id = "turn"
            self.started_messages = []

        async def steer_or_start(self, message):
            raise AppServerError("{'code': -32600, 'message': 'no active turn to steer'}")

        async def start_turn(self, message):
            self.started_messages.append(message)
            self.active_turn_id = "new-turn"
            return "new-turn"

    coder = FakeCoder()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = coder
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 55,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    health = store.get_health()
    assert controller.client.responses == [(55, {"decision": "decline"})]
    assert health.denied_requests == 1
    assert health.last_denial == "writes to supervisor runtime/state files are denied"
    assert coder.started_messages == ["writes to supervisor runtime/state files are denied"]
    assert store.get_bello_config().active_coder_turn_id == "new-turn"
    assert "started a new coder turn with the denial reason" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_approval_accept_does_not_steer_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 53,
                "method": "item/fileChange/requestApproval",
                "params": {"grantRoot": str(tmp_path / "src.py"), "availableDecisions": ["accept", "decline"]},
            }
        )
    )

    assert controller.client.responses == [(53, {"decision": "accept"})]
    assert controller.coder.messages == []


async def test_execpolicy_amendment_approval_is_not_rendered_as_denied(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )
    amendment = ["/bin/zsh", "-lc", "printf 'hello bello\\n' > hello.txt"]
    offered_decision = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}

    class FakeSupervisor:
        async def decide_approval(self, context, reason):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.APPROVE,
                approval_decision=ApprovalDecisionKind.ACCEPT,
                execpolicy_amendment=amendment,
                reason="scoped task file write",
            )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path, supervisor=FakeSupervisor())
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 54,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "command": "printf 'hello bello\\n' > hello.txt",
                    "cwd": str(tmp_path),
                    "availableDecisions": [offered_decision, "decline"],
                },
            }
        )
    )

    assert controller.client.responses == [(54, {"decision": offered_decision})]
    assert controller.tui.messages[0][0] == "APPROVAL"
    assert controller.coder.messages == []


async def test_run_shutdown_after_final_report_stops_stubbed_appserver(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class ShutdownClient:
        def __init__(self) -> None:
            self.initial_turn_started = asyncio.Event()
            self.stopped = False
            self.thread_count = 0

        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            self.stopped = True

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {
                "data": [
                    {"id": DEFAULT_MODEL},
                    {"id": "gpt-coder"},
                    {"id": "gpt-runtime"},
                    {"id": "gpt-completion"},
                ]
            }

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params, **kwargs):
            self.thread_count += 1
            return {
                "thread": {"id": f"thread-{self.thread_count}"},
                "approvalPolicy": "on-request",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }

        async def thread_unsubscribe(self, thread_id, **kwargs):
            return {}

        async def turn_start(self, params, **kwargs):
            self.initial_turn_started.set()
            return {"turn": {"id": "turn-1", "status": "running"}}

    client = ShutdownClient()
    monkeypatch.setattr("supervisor.controller._run_probe", lambda args: (True, "codex-cli test"))
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model="gpt-coder",
        runtime_model="gpt-runtime",
        completion_model="gpt-completion",
        coder_intelligence="ultra",
        runtime_intelligence="xhigh",
        completion_intelligence="high",
        adversary_enabled=False,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    # This test owns a deliberately minimal app-server stub and verifies coder
    # shutdown, not cheap-runtime startup.  Isolate the unrelated triage probe
    # so its turn cannot satisfy ``initial_turn_started`` first.
    controller._configure_runtime_triage = _async_noop

    run_task = asyncio.create_task(controller.run())
    await asyncio.wait_for(client.initial_turn_started.wait(), timeout=5)
    await controller.finalize("task complete", status=BelloStatus.COMPLETE)
    await asyncio.wait_for(run_task, timeout=5)

    assert controller.coder is not None
    assert controller.coder.model == "gpt-coder"
    assert controller.coder.intelligence == "ultra"
    assert controller.supervisor is not None
    assert controller.supervisor.model == "gpt-runtime"
    assert controller.supervisor.intelligence == "xhigh"
    assert controller.completion_supervisor is not None
    assert controller.completion_supervisor is not controller.supervisor
    assert controller.completion_supervisor.model == "gpt-completion"
    assert controller.completion_supervisor.intelligence == "high"
    assert controller.adv_report_controller is not None
    assert controller.adv_report_controller is not controller.completion_supervisor
    assert controller.adv_report_controller.model == "gpt-completion"
    assert controller.adv_report_controller.intelligence == "high"
    assert client.stopped is True
    assert controller.running is False


async def test_finalize_writes_report_and_status_before_terminal_shutdown(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    shutdown_seen = False

    async def fake_prepare_terminal_shutdown(reason: str) -> None:
        nonlocal shutdown_seen
        shutdown_seen = True
        assert store.get_bello_config().status == BelloStatus.COMPLETE
        report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
        assert "# Final Report" in report
        assert "task complete" in report

    controller._prepare_terminal_shutdown = fake_prepare_terminal_shutdown  # type: ignore[method-assign]

    await controller.finalize("task complete", status=BelloStatus.COMPLETE)

    assert shutdown_seen is True
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8").strip()


def test_run_async_cleanly_exits_zero_after_loop_cleanup() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_async_cleanly(_async_noop())

    assert exc_info.value.code == 0


async def _async_noop() -> None:
    return None


async def _async_schema_hash() -> str:
    return "schema"


class _GateFakeCoder:
    def __init__(self) -> None:
        self.messages = []

    async def steer_or_start(self, message):
        self.messages.append(message)
        return "turn"


class _FakeAdvReportController:
    def __init__(
        self,
        decisions: list[AdvReportControllerDecision] | None = None,
    ) -> None:
        self.decisions = list(decisions or [])
        self.packets: list[SupervisorWakePacket] = []

    async def decide_adv_report(
        self,
        packet: SupervisorWakePacket,
    ) -> AdvReportControllerDecision:
        self.packets.append(packet)
        if self.decisions:
            return self.decisions.pop(0)
        return AdvReportControllerDecision(
            forward_to_coder=False,
            reason="no findings or observations remained",
            report_to_coder=None,
        )


def _completion_gate_controller(
    tmp_path: Path,
    *,
    validations: list[ValidationRun],
) -> tuple[BelloController, StateStore, Path, _GateFakeCoder]:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    coder = _GateFakeCoder()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = None
    controller.adv_report_controller = _FakeAdvReportController()
    controller.coder = coder
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = validations
    controller.inspections = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.paused = False
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_task = None
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0
    controller.provider_failure_recovery_counts = {}
    controller.validation_runtime_state = {}
    controller.completion_review_return_sequence = None
    controller._terminal_cleanup_started = False
    controller._command_output_chunks = {}
    controller._last_large_diff_signature = None
    controller._last_restart_budget_signature = None
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    return controller, store, task, coder


class _RuntimeFakeSupervisor:
    def __init__(self, store: StateStore, task: Path) -> None:
        self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
        self.runtime_packets = []
        self.completion_packets = []
        self.completion_thread_id = None
        self.closed_completion_reviews = 0
        self.runtime_decision_kind = SupervisorDecisionKind.NOOP
        self.before_runtime_decision = None
        self.runtime_thread_id = None
        self.on_thread_start = None

    def build_packet(self, **kwargs):
        return self.agent.build_packet(**kwargs)

    async def decide(self, packet):
        self.runtime_packets.append(packet)
        if self.runtime_thread_id is not None and self.on_thread_start is not None:
            self.on_thread_start(self.runtime_thread_id)
        if self.before_runtime_decision is not None:
            pending = self.before_runtime_decision()
            if pending is not None:
                await pending
        return SupervisorDecision(
            decision=self.runtime_decision_kind,
            reason="observed",
            message_to_coder=(
                "Run a task-relevant behavioral validation."
                if self.runtime_decision_kind == SupervisorDecisionKind.INTERVENE
                else None
            ),
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    async def decide_completion(self, packet):
        self.completion_packets.append(packet)
        return CompletionReviewDecision(
            decision="return",
            reason="not used",
            uncovered_behaviors=[],
            validation_gaps=["fake completion gap"],
            claim_evidence_mismatches=[],
            packet_or_access_limitations=[],
            changed_test_risks=[],
            message_to_coder="not used",
            persistent_decision=None,
            progress_update=None,
            clear_handoff=False,
            display_message=None,
            handoff=None,
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    async def close_completion_review(self):
        self.closed_completion_reviews += 1
        self.completion_thread_id = None
        return None


class _CheapRuntimeNoopReviewer:
    model = "cheap-runtime-test"

    def __init__(self) -> None:
        self.calls = []

    async def review(self, packet):
        self.calls.append(packet)
        return CheapRuntimeDecision(decision="noop", reason_code="routine_progress")


def _runtime_controller(tmp_path: Path) -> tuple[BelloController, StateStore, _RuntimeFakeSupervisor]:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    fake = _RuntimeFakeSupervisor(store, task)
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = fake
    controller.adv_report_controller = _FakeAdvReportController()
    controller.coder = None
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.inspections = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.paused = False
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_task = None
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._current_turn_action_count = 0
    controller._last_completion_marker_sequence = None
    controller.no_marker_idle_nudge_count = 0
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.validation_runtime_state = {}
    controller.provider_failure_recovery_counts = {}
    controller.completion_review_return_sequence = None
    controller._terminal_cleanup_started = False
    controller._command_output_chunks = {}
    controller._last_large_diff_signature = None
    controller._last_restart_budget_signature = None
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    fake.on_thread_start = controller._register_reviewer_thread
    return controller, store, fake


def _runtime_controller_with_plan(
    tmp_path: Path,
    plan_text: str,
):
    controller, store, fake = _runtime_controller(tmp_path)
    plan = tmp_path / "PLAN.md"
    plan.write_text(plan_text, encoding="utf-8")
    snapshot = create_workspace_snapshot(
        tmp_path,
        controller.task_path,
        plan_path=plan,
    )
    controller.plan_path = plan.resolve()
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.declared_grading_roots = ()
    return controller, store, fake, snapshot, plan


async def test_controller_idle_guard_forces_completion_review_for_stalled_no_active_turn(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        active_turn_id = None

        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    coder = FakeCoder()
    controller.coder = coder
    controller.running = True
    controller._last_controller_activity_monotonic = 0.0
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "status": BelloStatus.RUNNING,
                "last_event_sequence": 17,
                "active_coder_turn_id": None,
            }
        )
    )

    await controller._handle_controller_idle_guard(now=301.0)
    await controller._supervisor_task

    assert coder.messages == ["not used"]
    assert coder.messages != [NO_MARKER_IDLE_NUDGE]
    assert len(fake.completion_packets) == 1
    log = store.path(LOG).read_text(encoding="utf-8")
    assert '"type": "controller_idle_guard"' in log


def _covered_accept_decision(*, wake_sequence: int, validation_id: str = "validation-3") -> CompletionReviewDecision:
    return CompletionReviewDecision.model_validate(
        {
            "decision": "accept",
            "reason": "covered",
            "files_reviewed": [
                {"path": "src/app.py", "reason": "changed source", "kind": "source", "inspected": True, "limitation": None},
                {"path": "tests/test_app.py", "reason": "changed test", "kind": "test", "inspected": True, "limitation": None},
            ],
            "behavior_evidence_matrix": [
                {
                    "behavior": "requested behavior",
                    "task_basis": "TASK.md",
                    "files_considered": ["src/app.py", "tests/test_app.py"],
                    "evidence": [
                        {
                            "validation_id": validation_id,
                            "command": "pytest tests/test_app.py",
                            "sequence": 3,
                            "validation_type": "behavioral",
                            "outcome": "pass",
                            "freshness": "fresh",
                            "why_it_covers_behavior": "executes the changed behavior",
                        }
                    ],
                    "status": "covered",
                    "gap": None,
                }
            ],
            "uncovered_behaviors": [],
            "validation_gaps": [],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": None,
            "persistent_decision": None,
            "progress_update": "Accepted by completion review.",
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": wake_sequence,
            "generation": 0,
        }
    )


def _gate_packet(
    task: Path,
    *,
    validations: list[ValidationRun],
    wake_sequence: int = 1,
    latest_change: int | None = 2,
) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=wake_sequence,
        latest_event_sequence=wake_sequence,
        generation=0,
        restart_count=0,
        task_path=str(task),
        task_contents=task.read_text(encoding="utf-8"),
        coder_thread_id="thread",
        changed_files=[
            ChangedFile(path="src/app.py", status="M", sequence=2),
            ChangedFile(path="tests/test_app.py", status="M", sequence=2),
        ],
        validations=validations,
        latest_relevant_change_sequence=latest_change,
    )


async def test_completion_return_with_delta_evidence_goes_to_coder(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            validation_id="validation-old",
            command="pytest tests/public",
            exit_code=0,
            passed=True,
            summary="old public pass",
            sequence=5,
        ),
        ValidationRun(
            validation_id="validation-demo",
            command="BELLO_BEHAVIOR_DEMO=1 ./c_compiler sample.c",
            exit_code=0,
            type="behavior_demo",
            passed=True,
            trusted_validation_outcome="passed",
            summary="returns 42",
            captured_output="program exit=42\n",
            sequence=15,
        ),
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations, wake_sequence=20)
    packet.completion_payload_mode = "delta"
    packet.completion_payload_since_sequence = 10
    decision = CompletionReviewDecision.model_validate(
        {
            "decision": "return",
            "reason": "old gap still lacks proof",
            "files_reviewed": [],
            "behavior_evidence_matrix": [],
            "uncovered_behaviors": [],
            "validation_gaps": ["needs direct behavior evidence"],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "provide direct behavior evidence",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 20,
            "generation": 0,
        }
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert coder.messages == ["provide direct behavior evidence"]
    assert len(controller.completion_returns) == 1
    assert "completion_return_freshness_failure" not in store.path(LOG).read_text(encoding="utf-8")


class _FakeTUI:
    def __init__(self) -> None:
        self.messages = []
        self.input_queue = asyncio.Queue()

    def render(self, title, message):
        self.messages.append((title, message))

    def status(self, message):
        self.messages.append(("STATUS", message))

    async def start(self):
        self.messages.append(("START", ""))

    async def stop(self):
        self.messages.append(("STOP", ""))


def test_adversary_snapshot_gets_functional_git_repo(tmp_path: Path) -> None:
    import shutil as _shutil
    import subprocess as _subprocess

    from supervisor.controller import _create_adversary_snapshot

    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("print('x')\n", encoding="utf-8")
    notes = project / "notes"
    notes.mkdir()
    (notes / "PLAN.md").write_text("PRIVATE PLAN\n", encoding="utf-8")
    (notes / "keep.md").write_text("public project note\n", encoding="utf-8")

    snapshot = _create_adversary_snapshot(
        project,
        excluded_relative_paths=("notes/PLAN.md",),
    )
    try:
        assert (snapshot / "app.py").exists()
        assert not (snapshot / "notes" / "PLAN.md").exists()
        assert (snapshot / "notes" / "keep.md").read_text(encoding="utf-8") == (
            "public project note\n"
        )
        assert (snapshot / ".git").is_dir()
        head = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=snapshot, capture_output=True, text=True
        )
        assert head.returncode == 0
        status = _subprocess.run(
            ["git", "status", "--short"], cwd=snapshot, capture_output=True, text=True
        )
        assert status.returncode == 0
        # Files stay untracked on purpose: recursive deletes inside the snapshot must
        # remain approvable for the adversary (tracked paths would be policy-denied).
        assert "?? app.py" in status.stdout
    finally:
        _shutil.rmtree(snapshot.parent, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink sanitization regression")
def test_adversary_snapshot_drops_link_that_escapes_workspace(tmp_path: Path) -> None:
    from supervisor.controller import _create_adversary_snapshot
    from supervisor.workspace_snapshot import remove_isolated_workspace_tree

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("host data\n", encoding="utf-8")
    link = project / "escape"
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    snapshot = _create_adversary_snapshot(project)
    try:
        assert not (snapshot / "escape").exists()
        assert not (snapshot / "escape").is_symlink()
        assert external.read_text(encoding="utf-8") == "host data\n"
    finally:
        remove_isolated_workspace_tree(snapshot.parent)


def test_adversary_snapshot_git_ignores_global_template_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor.controller import _create_adversary_snapshot

    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("print('x')\n", encoding="utf-8")
    marker = tmp_path / "global-hook-ran"
    template = tmp_path / "git-template"
    hooks = template / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    global_config = tmp_path / "global-gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(global_config), "init.templateDir", str(template)],
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    snapshot = _create_adversary_snapshot(project)
    try:
        assert not marker.exists()
        assert not (snapshot / ".git" / "hooks" / "post-commit").exists()
        hooks_path = subprocess.check_output(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=snapshot,
            text=True,
        ).strip()
        assert hooks_path == os.devnull
    finally:
        import shutil as _shutil

        _shutil.rmtree(snapshot.parent, ignore_errors=True)


def test_workspace_state_id_does_not_open_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not supported on this platform")
    from supervisor.controller import _workspace_state_id

    fifo = tmp_path / "coder-output"
    os.mkfifo(fifo)

    fifo_state = _workspace_state_id(tmp_path)
    fifo.unlink()
    fifo.write_text("regular file\n", encoding="utf-8")
    file_state = _workspace_state_id(tmp_path)

    assert fifo_state != file_state


def test_workspace_state_id_does_not_traverse_simulated_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor.controller import _workspace_state_id

    junction = tmp_path / "junction"
    junction.mkdir()
    outside_contents = junction / "outside.txt"
    outside_contents.write_text("first\n", encoding="utf-8")
    real_is_link = controller_module.is_link_or_reparse
    monkeypatch.setattr(
        controller_module,
        "is_link_or_reparse",
        lambda path, stat_result=None: path == junction
        or real_is_link(path, stat_result=stat_result),
    )

    before = _workspace_state_id(tmp_path)
    outside_contents.write_text("second\n", encoding="utf-8")
    after = _workspace_state_id(tmp_path)

    assert before == after


def test_workspace_context_reader_and_hasher_reject_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not supported on this platform")
    fifo = tmp_path / "coder-output"
    os.mkfifo(fifo)

    assert _read_workspace_file(tmp_path, "coder-output", limit=1000) is None
    with pytest.raises(OSError, match="not a regular file"):
        _hash_file(fifo)


def test_effective_max_adversary_runs_cli_override(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    # No overrides: falls back to the persisted config (default 1).
    controller.adversary_enabled = None
    assert controller._effective_max_adversary_runs() == 1

    # CLI --adversary true --adversary-runs 3: budget honored without touching persisted config.
    controller.adversary_enabled = True
    controller.adversary_runs = 3
    assert controller._effective_max_adversary_runs() == 3
    assert store.get_bello_config().max_adversary_runs == 1

    # CLI --adversary false wins regardless of budget.
    controller.adversary_enabled = False
    assert controller._effective_max_adversary_runs() == 0


class _FakeSteerCoder:
    def __init__(self) -> None:
        self.steers: list[str] = []

    async def steer_or_start(self, message: str) -> None:
        self.steers.append(message)


async def test_no_marker_idle_skips_review_for_virgin_generation(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None, "last_event_sequence": 17})
    )
    controller._generation_has_coder_turn = False
    controller.coder = _FakeSteerCoder()

    await controller._handle_no_marker_idle()

    assert fake.completion_packets == []
    assert "Controller forcing completion_review" not in store.path(PROGRESS).read_text(encoding="utf-8")
    assert controller.coder.steers == [POST_RESTART_CONTINUE_NUDGE]


async def test_completion_restart_discarded_for_virgin_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    handoff = RestartHandoff(
        objective="task",
        restart_reason="recovery restart before any coder work",
        bad_pattern="none",
        known_evidence="handoff from prior generation",
        next_step="continue",
        recovery_signal="new coder work",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.coder = _FakeSteerCoder()
    controller.pending_approvals = {}
    controller.prior_interventions = []
    controller.validations = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0
    controller._generation_has_coder_turn = False

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="restart",
            reason="generation recovery before any new coder work",
            uncovered_behaviors=[],
            validation_gaps=["stale prior-generation state"],
            message_to_coder=None,
            persistent_decision=None,
            progress_update="Restarting into recovery.",
            clear_handoff=False,
            display_message=None,
            handoff=handoff,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    cfg = store.get_bello_config()
    assert cfg.generation == 0
    assert cfg.status not in (BelloStatus.STUCK, BelloStatus.RESTARTING)
    assert controller.completion_restarts == 0
    assert "Discarded completion restart" in store.path(PROGRESS).read_text(encoding="utf-8")
    events = store.path(EVENTS).read_text(encoding="utf-8")
    assert "completion/restart_discarded_virgin_generation" in events
    assert controller.coder.steers == [POST_RESTART_CONTINUE_NUDGE]
