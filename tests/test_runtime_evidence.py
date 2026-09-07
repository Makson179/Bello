from __future__ import annotations

from supervisor.controller import (
    BelloController,
    _inspection_from_action,
    _is_completed_action,
    _item_summary,
    _triggering_action_from_item,
    _validation_from_action,
)


def test_native_file_read_is_inspection_not_a_write_or_validation(tmp_path):
    path = str(tmp_path / "source.py")
    item = {"type": "fileRead", "tool": "read_file", "arguments": {"path": path, "offset": 1, "limit": 20},
            "cwd": str(tmp_path), "paths": [path], "status": "completed", "exitCode": 0,
            "aggregatedOutput": "1: def entry():\n2:     pass\n"}
    assert _is_completed_action(item)
    action = _triggering_action_from_item(item, item_id="read-1", summary=_item_summary(item))
    assert action.paths == [path]
    assert action.command is None
    inspection = _inspection_from_action(action, sequence=4, item=item)
    assert inspection.passed
    assert inspection.inspected_paths == [path]
    assert inspection.command.startswith("tool:read_file ")
    assert "def entry" in inspection.captured_output
    assert _validation_from_action(action, sequence=4, item=item) is None
    controller = object.__new__(BelloController)
    controller.observed_changed_files = {}
    controller._record_changed_files(action)
    assert controller.observed_changed_files == {}


def test_native_failed_read_does_not_become_successful_evidence():
    item = {"type": "fileRead", "tool": "read_file", "arguments": {"path": "missing.py"},
            "paths": ["missing.py"], "status": "failed", "exitCode": 1,
            "aggregatedOutput": "file not found"}
    action = _triggering_action_from_item(item, item_id="read-2", summary="read failed")
    inspection = _inspection_from_action(action, sequence=5, item=item)
    assert not inspection.passed
    assert inspection.outcome == "fail"
