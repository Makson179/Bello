"""Selective, read-only evidence for Completion Review.

The prompt contains the objective and an index, not the accumulated execution
history. These inputs belong to the supervisor, outside the reviewer's writable
solution copy, and live for the persistent review session.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from supervisor.filesystem_safety import is_link_or_reparse, remove_path_tree
from supervisor.schemas import SupervisorWakePacket


# Keep the task and accumulated obligations visible. Large evidence collections
# below are discovered through their indexes instead of copied into every turn.
_INLINE_FIELDS = (
    "task_path", "task_contents", "wake_sequence", "generation", "restart_count",
    "latest_event_sequence", "completion_attempt_count", "completion_returns_this_generation",
    "last_readiness_marker_sequence", "latest_relevant_change_sequence",
    "completion_payload_mode", "completion_payload_since_sequence",
    "behavior_surface", "prior_uncovered_edge_candidates",
)

_EVIDENCE_FIELDS = (
    "changed_files", "validations", "inspections", "validation_outputs", "inspection_outputs",
    "evidence_provenance_summary", "changed_tests_summary", "changed_file_diffs",
    "changed_file_contexts", "previous_completion_returns", "prior_interventions",
    "last_actions", "recent_events", "progress", "decisions", "health", "handoff",
    "last_coder_message", "human_message", "current_summary", "validation_freshness_summary",
    "completion_delta_evidence_summary", "diff_summary", "patch_summary",
    "diff_packet_limits", "breadth_risk_summary",
)

_INDEX_FIELDS = (
    "validation_id", "inspection_id", "sequence", "generation", "source", "path", "status",
    "type", "kind", "method", "command", "outcome", "trusted_validation_outcome", "passed",
    "masking_reason", "reason", "summary", "fresh_after_latest_relevant_change",
    "independence_class", "output_kind",
)


def _excerpt(value: str, limit: int = 240) -> str:
    return value if len(value) <= limit else value[:limit] + " … [full text in evidence file]"


class CompletionContextStore:
    """Own immutable per-wake input files until the reviewer thread is closed."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="bello-completion-inputs-")).resolve()
        self._files: dict[Path, str] = {}
        self._contexts: list[dict[str, Any]] = []

    def write_packet(self, packet: SupervisorWakePacket, *, task_in_file: bool = False) -> dict[str, Any]:
        # Never rewrite an earlier wake: a persistent reviewer can retain paths and
        # cached messages from it. Filenames never come from model/coder-controlled IDs.
        self.assert_unchanged()
        wake_root = Path(tempfile.mkdtemp(prefix=f"wake-{packet.wake_sequence}-", dir=self.root))
        raw = packet.model_dump(mode="json", exclude={"adversary_report"})
        payload = {key: raw[key] for key in _INLINE_FIELDS}
        task_path = self._write_text(wake_root / "TASK.md", packet.task_contents)
        payload["task_path"] = str(task_path)
        if task_in_file:
            payload.pop("task_contents")
        payload["current_summary"] = _excerpt(packet.current_summary, 1000)
        payload["validation_freshness_summary"] = _excerpt(packet.validation_freshness_summary or "", 1000)
        evidence: dict[str, Any] = {}
        for name in _EVIDENCE_FIELDS:
            value = raw[name]
            if value is None or value == "" or value == [] or value == {}:
                continue
            if isinstance(value, list):
                evidence[name] = self._write_collection(wake_root, name, value)
            elif isinstance(value, str):
                path = self._write_text(wake_root / f"{name}.txt", value)
                evidence[name] = {"path": str(path), "characters": len(value)}
            else:
                path = self._write_json(wake_root / f"{name}.json", value)
                evidence[name] = {"path": str(path)}
        payload["available_evidence"] = evidence
        payload["evidence_summary"] = _evidence_summary(packet)
        payload["review_context_mode"] = "selective_files"
        if self._contexts:
            prior_path = self._write_text(
                wake_root / "earlier_contexts.jsonl",
                "".join(json.dumps(item) + "\n" for item in self._contexts),
            )
            payload["earlier_evidence_contexts"] = {
                "index_path": str(prior_path), "count": len(self._contexts),
            }
        self._write_json(wake_root / "index.json", {
            "wake_sequence": packet.wake_sequence,
            "generation": packet.generation,
            "task_path": str(task_path),
            "available_evidence": evidence,
        })
        payload["evidence_index_path"] = str(wake_root / "index.json")
        self._contexts.append({
            "wake_sequence": packet.wake_sequence,
            "generation": packet.generation,
            "index_path": payload["evidence_index_path"],
        })
        wake_root.chmod(0o500)
        return payload

    def _write_collection(self, wake_root: Path, name: str, values: list[Any]) -> dict[str, Any]:
        directory = wake_root / name
        directory.mkdir(mode=0o700)
        index: list[dict[str, Any]] = []
        for position, value in enumerate(values):
            path = self._write_json(directory / f"{position:06d}.json", value)
            row: dict[str, Any] = {"entry": position, "path": str(path)}
            if isinstance(value, dict):
                # A source path is distinct from the actual evidence file path.
                for key in _INDEX_FIELDS:
                    if key in value:
                        item = value[key]
                        row["source_path" if key == "path" else key] = (
                            _excerpt(item) if isinstance(item, str) else item
                        )
            elif isinstance(value, str):
                row["excerpt"] = _excerpt(value)
            index.append(row)
        path = self._write_text(
            wake_root / f"{name}.jsonl",
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in index),
        )
        directory.chmod(0o500)
        return {"index_path": str(path), "count": len(values), "format": "jsonl_index"}

    def _write_json(self, path: Path, value: Any) -> Path:
        return self._write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _write_text(self, path: Path, text: str) -> Path:
        data = text.encode("utf-8")
        with path.open("xb") as handle:
            handle.write(data)
        path.chmod(0o400)
        self._files[path] = hashlib.sha256(data).hexdigest()
        return path

    def assert_unchanged(self) -> None:
        if is_link_or_reparse(self.root) or not self.root.is_dir():
            raise OSError("completion evidence root was removed or replaced")
        for path, expected in self._files.items():
            parent = path.parent
            while parent != self.root:
                if is_link_or_reparse(parent) or not parent.is_dir():
                    raise OSError("completion evidence directory was removed or replaced")
                parent = parent.parent
            if is_link_or_reparse(path) or not path.is_file():
                raise OSError("completion evidence file was removed or replaced")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise OSError("completion evidence was modified during review")

    def cleanup(self) -> None:
        remove_path_tree(self.root)
        self._files.clear()
        self._contexts.clear()


def _evidence_summary(packet: SupervisorWakePacket) -> dict[str, Any]:
    latest_change = packet.latest_relevant_change_sequence
    outcomes = Counter(item.trusted_validation_outcome for item in packet.validations)
    # These are ledger counts, NOT an accept verdict or a claim of test coverage.
    # Keep masking/freshness warnings visible before the reviewer selects details.
    fresh_passing = sum(
        item.trusted_validation_outcome == "passed"
        and item.type in {"behavioral", "behavior_demo"}
        and latest_change is not None
        and item.sequence > latest_change
        for item in packet.validations
    )
    provenance = packet.evidence_provenance_summary
    return {
        "changed_files": len(packet.changed_files),
        "validation_records": len(packet.validations),
        "inspection_records": len(packet.inspections),
        "validation_outcomes": dict(outcomes),
        "fresh_passing_behavioral_records": fresh_passing if latest_change is not None else None,
        "stale_validation_records": sum(item.sequence <= latest_change for item in packet.validations)
        if latest_change is not None else None,
        "provenance_flagged_records": sum(bool(item.risk_reasons) for item in provenance.validations)
        if provenance is not None else 0,
        "provenance_classes": dict(Counter(item.independence_class for item in provenance.validations))
        if provenance is not None else {},
        "capture_inconsistencies": len(provenance.capture_inconsistencies) if provenance is not None else 0,
        "changed_test_files": len(provenance.changed_test_files) if provenance is not None else 0,
        "previous_completion_returns": len(packet.previous_completion_returns),
        "human_message_available": packet.human_message is not None,
        "counts_are_not_coverage_or_acceptance": True,
    }
