"""Controller evidence regression tests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
import pytest
import supervisor.controller as controller_module
from supervisor.controller import BelloController, _ensure_internal_runtime_git_excluded, _git_status_entries_from_porcelain_v1_z, _hash_file, _path_from_git_status_line, _read_workspace_file, _evidence_provenance_summary, _file_kind
from supervisor.schemas import ChangedFile, BelloConfig, ValidationRun
from supervisor.state import StateStore
from supervisor.workspace_snapshot import create_workspace_snapshot

from tests.support.controller import (
    _runtime_controller,
)


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


def test_workspace_state_id_does_not_open_fifo(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not supported on this platform")
    from supervisor.controller import _workspace_state_id

    fifo = tmp_path / "coder-output"
    request.addfinalizer(lambda: fifo.unlink(missing_ok=True))
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


def test_workspace_context_reader_and_hasher_reject_fifo(
    tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO files are not supported on this platform")
    fifo = tmp_path / "coder-output"
    request.addfinalizer(lambda: fifo.unlink(missing_ok=True))
    os.mkfifo(fifo)

    assert _read_workspace_file(tmp_path, "coder-output", limit=1000) is None
    with pytest.raises(OSError, match="not a regular file"):
        _hash_file(fifo)
