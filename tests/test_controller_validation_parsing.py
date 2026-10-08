"""Controller validation parsing regression tests."""
from __future__ import annotations

from pathlib import Path
import pytest
import supervisor.controller as controller_module
from supervisor.controller import _has_passing_behavioral_validation, _validation_from_action
from supervisor.schemas import TriggeringAction

from tests.support.controller import (
    _runtime_controller,
)


@pytest.mark.parametrize("passed_count", [4, 5])
@pytest.mark.parametrize("reporter_prefix", ["ℹ", "#"])
def test_node_test_summary_is_not_confused_with_npm_package_version(
    tmp_path: Path,
    posix_command_semantics: None,
    passed_count: int,
    reporter_prefix: str,
) -> None:
    # Reproduces the npm header from native 2048 runs: its final version digit
    # is not the test count ("1.0.0 test" previously matched "0 test").
    output = (
        "> signalglass-2048@1.0.0 test\n> node --test\n"
        "✔ validates persisted games defensively (0.616583ms)\n"
        f"{reporter_prefix} tests {passed_count}\n"
        f"{reporter_prefix} suites 0\n"
        f"{reporter_prefix} pass {passed_count}\n"
        f"{reporter_prefix} fail 0\n"
        f"{reporter_prefix} cancelled 0\n"
        f"{reporter_prefix} skipped 0\n"
        f"{reporter_prefix} todo 0\n"
    )
    controller, store, _ = _runtime_controller(tmp_path)
    for sequence in (335, 432):
        validation = _validation_from_action(
            TriggeringAction(
                kind="commandExecution", command="/bin/zsh -c 'npm test'",
                exit_code=0, status="completed", summary="command completed",
            ),
            sequence=sequence,
            item={"aggregatedOutput": output},
        )
        assert validation is not None
        assert validation.type == "behavioral"
        assert validation.outcome == "pass"
        assert validation.trusted_validation_outcome == "passed"
        assert validation.passed is True
        assert validation.passed_count == passed_count
        assert validation.failed_count == 0
        assert controller._record_validation_runtime_state(validation) == ()
        controller._record_validation_progress(validation)
    assert store.get_bello_config().last_trusted_passing_behavioral_validation_sequence == 432


@pytest.mark.parametrize(
    ("output", "expected_passed", "expected_failed"),
    [
        ("ℹ tests 5\nℹ pass 4\nℹ fail 1\n", 4, 1),
        ("# tests 5\n# pass 4\n# fail 1\n", 4, 1),
        ("ℹ tests 0\nℹ pass 0\nℹ fail 0\n", 0, 0),
        ("# tests 0\n# pass 0\n# fail 0\n", 0, 0),
        ("0 tests executed\n", None, None),
    ],
)
def test_node_failed_or_empty_suite_is_not_a_successful_validation(
    output: str, expected_passed: int | None, expected_failed: int | None,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution", command="npm test", exit_code=0,
            status="completed", summary="command completed",
        ),
        sequence=1, item={"stdout": output},
    )
    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.outcome == "fail"
    assert validation.trusted_validation_outcome == "failed"
    assert validation.passed is False
    assert validation.passed_count == expected_passed
    assert validation.failed_count == expected_failed


@pytest.mark.parametrize(
    "command",
    [
        "/bin/zsh -c 'npm test && git diff --check'",
        "/bin/zsh -c 'npm test && node --check app.js && node --check game.js'",
        "node --check game.js && npm test",
        "git diff --check && npm test",
        "npm run lint; npm test",
    ],
)
def test_compound_test_and_static_checks_remain_behavioral(
    tmp_path: Path, posix_command_semantics: None, command: str,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution", command=command, exit_code=0,
            status="completed", summary="command completed",
        ),
        sequence=815,
        item={"stdout": (
            "> signalglass-2048@1.0.0 test\n> node --test\n"
            "✔ validates persisted games defensively (0.0925ms)\n"
            "ℹ tests 5\nℹ suites 0\nℹ pass 5\nℹ fail 0\n"
        )},
    )
    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "passed"
    controller, store, _ = _runtime_controller(tmp_path)
    controller._record_validation_progress(validation)
    assert store.get_bello_config().last_trusted_passing_behavioral_validation_sequence == 815


@pytest.mark.parametrize(
    "command",
    [
        "node --check game.test.js",
        "node --check game.test.js && git diff --check",
        "printf 'npm test' && node --check game.js",
        "cat npm-test.log && node --check game.js",
    ],
)
def test_compound_static_checks_do_not_invent_test_execution(
    posix_command_semantics: None, command: str,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution", command=command, exit_code=0,
            status="completed", summary="command completed",
        ),
        sequence=1, item={"stdout": ""},
    )
    assert validation is not None
    assert validation.type == "static"


@pytest.mark.parametrize(
    "command",
    [
        "false && npm test; git diff --check",
        "true || npm test; git diff --check",
    ],
)
def test_skipped_compound_test_branch_is_not_behavioral_evidence(
    posix_command_semantics: None, command: str,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution", command=command, exit_code=0,
            status="completed", summary="command completed",
        ),
        sequence=1, item={"stdout": ""},
    )
    assert validation is not None
    assert validation.type == "static"
    assert _has_passing_behavioral_validation([validation]) is False


@pytest.mark.parametrize(
    "command",
    [
        "/bin/zsh -lc \"printf '\\n===== test/game.test.js =====\\n'; sed -n '1,260p' 'test/game.test.js'\"",
        "/bin/zsh -lc 'git -c core.excludesFile=/dev/null diff -- game.js app.js styles.css index.html test/game.test.js'",
        "/bin/zsh -lc 'git --no-pager --no-optional-locks -c core.excludesfile=/dev/null -c global.excludesfile=/dev/null diff -- game.js app.js test/game.test.js styles.css package.json'",
        "/bin/zsh -lc \"sleep 2; stat -f '%Sm %N' -t '%H:%M:%S' game.js app.js test/game.test.js styles.css package.json\"",
        "printf '%s' ./test/game.test.js",
        "printf '%s' 'node --test'",
        "printf '%s' 'python -m pytest'",
        "printf '%s' ';' ./run_tests.sh",
        r"printf '%s' \; ./run_tests.sh",
        "sed -n '1,20p' ./run_tests.sh",
        "git diff -- ./run_tests.sh",
        "sleep 2; stat ./run_tests.sh",
        "command -v playwright || true",
        "command -v chromium || command -v playwright || true",
    ],
    ids=["saved-printf-sed", "saved-git-config", "saved-git-options", "saved-sleep-stat",
         "printf-path", "printed-node", "printed-pytest", "quoted-separator", "escaped-separator",
         "sed-path", "diff-path", "stat-path", "which-runner", "which-fallbacks"],
)
def test_test_names_in_inspection_arguments_do_not_create_trusted_validation(
    posix_command_semantics: None, command: str,
) -> None:
    # Even reading a log containing genuine runner output must not turn the
    # inspection into an executed test or advance trusted validation freshness.
    validation = _validation_from_action(
        TriggeringAction(kind="commandExecution", command=command, exit_code=0,
                         status="completed", summary="command completed"),
        sequence=850, item={"stdout": "# tests 5\n# pass 5\n# fail 0\n"},
        changed_paths=["game.js", "test/game.test.js"],
    )
    assert validation is None


@pytest.mark.parametrize(
    "command",
    [
        "./run_visible_tests.sh",
        "/bin/bash -lc ./run_visible_tests.sh",
        "sh ./run_tests.sh",
        "bash -eu ./run_tests.sh",
        "python3 -B ./test_game.py",
        "python3 -W ignore ./test_game.py",
        "python3 -X dev -m unittest -v",
        "node test/game.test.js",
        "node --no-warnings test/game.test.js",
        "ruby ./game_test.rb",
        "env CI=1 python3 -m pytest tests/test_game.py",
        "./node_modules/.bin/mocha test/game.test.js",
        "npx --no-install vitest run",
        "node --test",
        "cd app && node test/game.test.js",
        "printf '%s' 'test/game.test.js'; sh ./run_tests.sh",
        "sh ./run_tests.sh && git diff --check",
    ],
)
def test_test_wrappers_are_recognized_only_at_execution_positions(
    posix_command_semantics: None, command: str,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(kind="commandExecution", command=command, exit_code=0,
                         status="completed", summary="command completed"),
        sequence=900, item={"stdout": "# tests 5\n# pass 5\n# fail 0\n"},
    )
    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.trusted_validation_outcome == "passed"
    assert _has_passing_behavioral_validation([validation])


@pytest.mark.parametrize(
    "command",
    [
        "python3 -W test_game.py application.py",
        "python3 -c 'print(1)' test_game.py",
        "node -e 'console.log(1)' test/game.test.js",
        "node --check test/game.test.js",
        "node app.js --test test/game.test.js",
        "bash -n ./run_tests.sh",
    ],
)
def test_interpreter_flag_values_and_script_arguments_are_not_test_wrappers(
    posix_command_semantics: None, command: str,
) -> None:
    assert not controller_module._is_behavioral_validation_command(command)
    assert not controller_module._is_test_wrapper_script_command(command)


@pytest.mark.parametrize(
    "output",
    [
        "ℹ tests 5\nℹ pass 5\nℹ fail 0\n1 failed\n",
        "0 failed\nℹ tests 5\nℹ pass 4\nℹ fail 1\n",
        "ℹ tests 5\nℹ pass 5\nℹ fail 0\nℹ tests 5\nℹ pass 4\nℹ fail 1\n",
    ],
    ids=["node-pass-other-runner-fail", "other-runner-pass-node-fail", "node-pass-node-fail"],
)
def test_compound_node_summary_cannot_hide_a_failing_validation(
    posix_command_semantics: None, output: str,
) -> None:
    validation = _validation_from_action(
        TriggeringAction(
            kind="commandExecution", command="npm test; node --check game.js",
            exit_code=0, status="completed", summary="command completed",
        ),
        sequence=1, item={"stdout": output},
    )
    assert validation is not None
    assert validation.type == "behavioral"
    assert validation.failed_count == 1
    assert validation.outcome == "fail"
    assert validation.trusted_validation_outcome == "failed"
    assert validation.passed is False


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
