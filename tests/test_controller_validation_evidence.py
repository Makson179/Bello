"""Controller validation evidence regression tests."""
from __future__ import annotations

import sys
from pathlib import Path
from supervisor.controller import _has_passing_behavioral_validation, _inspection_from_action, _evidence_provenance_summary, _validation_from_action
from supervisor.schemas import ChangedFile, TriggeringAction


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
        *(path.read_text(encoding="utf-8")
          for path in sorted((root / "supervisor" / "controller_parts").glob("*.py"))),
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
