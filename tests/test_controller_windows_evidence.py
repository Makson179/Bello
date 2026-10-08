"""Controller windows evidence regression tests."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
import supervisor.controller as controller_module
import supervisor.policy as policy_module
from supervisor.controller import _canonical_restart_command, _has_passing_behavioral_validation, _inspection_from_action, _validation_from_action
from supervisor.appserver import AppServerMessage
from supervisor.schemas import TriggeringAction
from supervisor.state import RUNTIME_TRACE

from tests.support.controller import (
    _runtime_controller,
)


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
