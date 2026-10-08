"""Controller profiles regression tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from supervisor.controller import BelloController, _sandbox_matches_mode
from supervisor.coder import CODEX_FAST_SERVICE_TIER, CoderSession, build_multi_agent_developer_instructions, coder_thread_params, coder_thread_resume_params, coder_turn_params
from supervisor.project_config import MODEL_GPT_5_6_LUNA, MODEL_GPT_5_6_TERRA, MultiAgentConfig, SubagentDefaultConfig
from supervisor.schemas import BelloConfig
from supervisor.state import StateStore


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
        intelligence="high",
        multi_agent=config,
    )

    assert params["threadId"] == "thread-1"
    assert params["cwd"] == str(tmp_path.resolve())
    assert params["approvalPolicy"] == "on-request"
    assert params["approvalsReviewer"] == "user"
    assert params["model"] == MODEL_GPT_5_6_TERRA
    assert params["effort"] == "high"
    assert params["config"]["agents"]["enabled"] is True
    assert params["config"]["agents"]["max_concurrent_threads_per_session"] == 3


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


def test_coder_thread_and_turn_params_include_intelligence_effort(tmp_path: Path) -> None:
    assert coder_thread_params(tmp_path, intelligence="xhigh")["effort"] == "xhigh"
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
            "allowed_profiles": {model: list(efforts) for model, efforts in multi_agent.allowed.items()},
            "role": "coder",
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
    assert client.thread_params["effort"] == "xhigh"
    assert "developerInstructions" in client.thread_params
    assert client.turn_params["input"] == [
        {"type": "text", "text": "unchanged user prompt", "text_elements": []}
    ]
    assert "developerInstructions" not in client.turn_params
    assert "config" not in client.turn_params
    assert client.turn_params["runtimeWorkspaceRoots"] == [str(tmp_path.resolve())]


def test_coder_sandbox_can_use_danger_full_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "danger-full-access")

    assert coder_thread_params(tmp_path)["sandbox"] == "danger-full-access"
    assert coder_turn_params("thread", "work", tmp_path)["sandboxPolicy"] == {"type": "dangerFullAccess"}
