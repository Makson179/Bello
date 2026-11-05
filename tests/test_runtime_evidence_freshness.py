"""Offline metadata-only freshness labels; no file-hash delivery gate."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from supervisor.runtime_evidence import inspection_staleness

def inspection():
    return SimpleNamespace(passed=True, sequence=10, inspected_paths=["candidate.py"], cwd=None)

def test_later_matching_edit_marks_historical_read_stale(tmp_path):
    assert inspection_staleness(inspection(), [{"path": "candidate.py", "sequence": 11}], workspace_root=tmp_path) == "stale"

@pytest.mark.parametrize("changes", [[], [{"path": "candidate.py", "sequence": 9}], [{"path": "other.py", "sequence": 99}]])
def test_earlier_or_unrelated_edit_never_proves_read_current(tmp_path, changes):
    assert inspection_staleness(inspection(), changes, workspace_root=tmp_path) == "historical"

def test_unknown_matching_edit_sequence_does_not_claim_freshness(tmp_path):
    assert inspection_staleness(inspection(), [{"path": "candidate.py", "sequence": None}], workspace_root=tmp_path) == "unknown"

def test_relative_inspection_cwd_matches_workspace_relative_change(tmp_path):
    read = {"passed": True, "sequence": 10, "inspected_paths": ["candidate.py"], "cwd": str(tmp_path / "src")}
    assert inspection_staleness(read, [{"path": "src/candidate.py", "sequence": 11}], workspace_root=tmp_path) == "stale"

@pytest.mark.parametrize("read_path,edit_path", [("src", "src/a.py"), ("src/a.py", "src")])
def test_directory_read_or_replacement_conservatively_stale(tmp_path, read_path, edit_path):
    read = {"passed": True, "sequence": 10, "inspected_paths": [read_path]}
    assert inspection_staleness(read, [{"path": edit_path, "sequence": 11}], workspace_root=tmp_path) == "stale"

@pytest.mark.parametrize("read", [
    {"passed": True, "sequence": 10, "inspected_paths": []},
    {"passed": False, "sequence": 10, "inspected_paths": ["candidate.py"]},
    {"passed": True, "sequence": None, "inspected_paths": ["candidate.py"]},
    {"passed": True, "sequence": 10, "inspected_paths": ["../outside.py"]}])
def test_missing_or_failed_inspection_metadata_is_unknown(tmp_path, read):
    assert inspection_staleness(read, [], workspace_root=tmp_path) == "unknown"

def test_staleness_annotation_does_not_read_source(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata labels must not read or hash source.")
    for method in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, method, forbidden)
    assert inspection_staleness(inspection(), [{"path": "candidate.py", "sequence": 11}], workspace_root=tmp_path) == "stale"
