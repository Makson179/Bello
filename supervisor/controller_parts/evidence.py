"""Evidence service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from supervisor.process_fence import create_subprocess_exec
from typing import Any

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class EvidenceState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    observed_changed_files: dict[str, compat.ChangedFile]


class ChangedFileBatch(list):
    """A legacy-compatible list with omission metadata for this discovery.

    Packet construction can await other work. Keeping overflow on the result
    prevents a concurrent discovery from replacing the earlier packet's limits.
    This is metadata only: omitted files are never loaded or paginated.
    """

    __slots__ = ("_omitted",)

    def __init__(
        self,
        included: list[compat.ChangedFile],
        omitted: list[compat.ChangedFile],
    ) -> None:
        super().__init__(included)
        self._omitted = tuple(omitted)

    @property
    def omitted(self) -> tuple[compat.ChangedFile, ...]:
        return self._omitted


class EvidencePort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_active_task_path',
        '_active_workspace_root',
        '_canonical_task_text',
        '_changed_file_diff',
        '_coder_snapshot',
        '_exposes_review_private_input',
        '_git_command_excluding_review_private_inputs',
        '_git_output',
        '_is_git_work_tree',
        '_is_review_private_path',
        '_review_private_relative_paths',
        '_review_safe_values',
        '_sequence',
        'inspections',
        'observed_changed_files',
        'project_root',
        'task_path',
        'use_git_diff',
        'validations',
    })
    writes = frozenset({
    })


class Evidence:
    """Own evidence behavior; borrow only the declared port."""

    def __init__(self, ports: EvidencePort) -> None:
        self.state = EvidenceState()
        self.ports = ports

    def _review_private_relative_paths(self) -> tuple[str, ...]:
        snapshot = getattr(self.ports, "_coder_snapshot", None)
        plan_relative_path = getattr(snapshot, "plan_relative_path", None)
        if not plan_relative_path:
            return ()
        return (str(plan_relative_path),)

    def _is_review_private_path(self, path: str) -> bool:
        normalized = compat._normalize_internal_workspace_path(path)
        private_paths = {
            compat._normalize_internal_workspace_path(private_path)
            for private_path in self.ports._review_private_relative_paths()
        }
        if compat.is_windows_platform():
            normalized = normalized.casefold()
            private_paths = {private_path.casefold() for private_path in private_paths}
        return normalized in private_paths

    def _exposes_review_private_input(self, value: compat.Any) -> bool:
        """Return whether structured evidence names a coder-only input path.

        Direct provenance is path-based.  Output/message fields additionally reject
        the complete plan payload so an aggregate or glob read cannot forward it.  A
        plan may contain an ordinary command such as ``pytest -q``; individual plan
        lines are never matched against commands, so genuine validation using the same
        words remains independent evidence.
        """

        snapshot = getattr(self.ports, "_coder_snapshot", None)
        if snapshot is None or not getattr(snapshot, "plan_relative_path", None):
            return False
        structured_value = value
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        text_parts: list[str] = []

        def collect_strings(candidate: compat.Any, *, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(candidate, str):
                text_parts.append(candidate)
                return
            if isinstance(candidate, bytes):
                text_parts.append(candidate.decode("utf-8", errors="replace"))
                return
            if isinstance(candidate, dict):
                for key, nested in candidate.items():
                    collect_strings(key, depth=depth + 1)
                    collect_strings(nested, depth=depth + 1)
                return
            if isinstance(candidate, (list, tuple, set)):
                for nested in candidate:
                    collect_strings(nested, depth=depth + 1)
                return
            if isinstance(candidate, compat.Path):
                text_parts.append(str(candidate))

        collect_strings(value)
        case_insensitive_paths = compat.is_windows_platform()

        def comparable_path_text(value: str) -> str:
            return value.casefold() if case_insensitive_paths else value

        comparable = comparable_path_text("\n".join(text_parts))

        path_markers: set[str] = set()
        for candidate in (
            getattr(snapshot, "plan_relative_path", None),
            getattr(snapshot, "plan_path", None),
            getattr(snapshot, "plan_source_path", None),
        ):
            if candidate is None:
                continue
            marker = str(candidate)
            path_markers.add(marker)
            path_markers.add(marker.replace("/", "\\"))
            path_markers.add(marker.replace("\\", "/"))
        relative_marker = str(snapshot.plan_relative_path)
        path_markers.add(f"./{relative_marker}")
        windows_relative_marker = relative_marker.replace("/", "\\")
        path_markers.add(f".\\{windows_relative_marker}")
        def contains_path_token(marker: str) -> bool:
            candidate = comparable_path_text(marker)
            if not candidate:
                return False
            return compat.re.search(
                rf"(?<![\w./\\-]){compat.re.escape(candidate)}(?![\w./\\-])",
                comparable,
            ) is not None

        def contains_path_component(component: str) -> bool:
            candidate = comparable_path_text(component)
            if not candidate:
                return False
            return compat.re.search(
                rf"(?<![\w.-]){compat.re.escape(candidate)}(?![\w.-])",
                comparable,
            ) is not None

        for marker in path_markers:
            if contains_path_token(marker):
                return True

        # A shell action can name a nested plan relative to its own cwd, for
        # example ``cwd=.../docs`` with ``cat PLAN.md``.  Do not match the
        # basename globally: require the relative parent components to be present
        # in the same structured action as well.
        plan_relative = compat.Path(str(snapshot.plan_relative_path))
        parent_parts = tuple(
            comparable_path_text(part)
            for part in plan_relative.parent.parts
            if part not in {"", "."}
        )
        basename = comparable_path_text(plan_relative.name)
        if (
            parent_parts
            and basename
            and contains_path_token(basename)
            and all(contains_path_component(part) for part in parent_parts)
        ):
            return True

        # Indirect reads (for example a Markdown glob) need not spell the plan
        # path.  Inspect only output/message-bearing fields for the complete plan
        # payload; never compare plan lines against command fields.
        private_texts: list[str] = []
        if isinstance(structured_value, (compat.ValidationRun, compat.InspectionRun)):
            private_texts.append(structured_value.captured_output)
        elif isinstance(structured_value, compat.CoderMessage):
            private_texts.append(structured_value.text)
        elif isinstance(structured_value, compat.SubagentSummary):
            private_texts.extend(
                text
                for text in (
                    structured_value.prompt,
                    structured_value.last_message,
                    *(activity.summary for activity in structured_value.recent_actions),
                )
                if text
            )
        elif isinstance(structured_value, compat.PriorIntervention):
            private_texts.extend(
                (structured_value.reason, structured_value.message_to_coder)
            )
        elif isinstance(value, dict):
            for key in (
                "captured_output",
                "output",
                "text",
                "prompt",
                "last_message",
                "message_to_coder",
            ):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    private_texts.append(candidate)
        plan_bytes = getattr(snapshot, "plan_bytes", None)
        if isinstance(plan_bytes, bytes) and plan_bytes and private_texts:
            def normalized_newlines(text: str) -> str:
                return text.replace("\r\n", "\n").replace("\r", "\n")

            plan_text = normalized_newlines(
                plan_bytes.decode("utf-8", errors="replace")
            )
            private_texts = [normalized_newlines(text) for text in private_texts]
            markers = {plan_text, plan_text.strip()}
            if len(plan_text) > 512:
                markers.update(
                    {
                        plan_text[:256],
                        plan_text[len(plan_text) // 2 - 128 : len(plan_text) // 2 + 128],
                        plan_text[-256:],
                    }
                )
            markers = {marker for marker in markers if marker.strip()}
            if any(
                marker in candidate
                for marker in markers
                for candidate in private_texts
            ):
                return True
        return False

    def _review_safe_values(self, values: list[compat.Any]) -> list[compat.Any]:
        return [
            value
            for value in values
            if not self.ports._exposes_review_private_input(value)
        ]

    def _review_safe_packet_state(
        self,
        packet: compat.SupervisorWakePacket,
    ) -> compat.SupervisorWakePacket:
        """Remove runtime-authored state that would disclose coder-only plan input."""

        def exposes_freeform(value: compat.Any) -> bool:
            if value is None:
                return False
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            text_parts: list[str] = []

            def collect(candidate: compat.Any) -> None:
                if isinstance(candidate, str):
                    text_parts.append(candidate)
                elif isinstance(candidate, bytes):
                    text_parts.append(candidate.decode("utf-8", errors="replace"))
                elif isinstance(candidate, dict):
                    for key, nested in candidate.items():
                        collect(key)
                        collect(nested)
                elif isinstance(candidate, (list, tuple, set)):
                    for nested in candidate:
                        collect(nested)
                elif isinstance(candidate, compat.Path):
                    text_parts.append(str(candidate))

            collect(value)
            return self.ports._exposes_review_private_input(
                {"text": "\n".join(text_parts)}
            )

        updates: dict[str, compat.Any] = {}
        for field_name in ("progress", "decisions", "current_summary"):
            value = getattr(packet, field_name)
            if exposes_freeform(value):
                updates[field_name] = (
                    "Coder work is ready for independent review."
                    if field_name == "current_summary"
                    else ""
                )
        if exposes_freeform(packet.handoff):
            updates["handoff"] = None
        if exposes_freeform(packet.health):
            updates["health"] = {}
        updates["last_actions"] = [
            value for value in packet.last_actions if not exposes_freeform(value)
        ]
        updates["recent_events"] = [
            value for value in packet.recent_events if not exposes_freeform(value)
        ]
        return packet.model_copy(update=updates)

    def _git_command_excluding_review_private_inputs(
        self,
        command: list[str],
    ) -> list[str]:
        private_paths = self.ports._review_private_relative_paths()
        if not private_paths or tuple(command[:2]) not in {
            ("git", "status"),
            ("git", "diff"),
        }:
            return list(command)
        filtered = list(command)
        if "--" not in filtered:
            filtered.append("--")
        filtered.append(".")
        filtered.extend(
            f":(exclude,top,literal){private_path}"
            for private_path in private_paths
        )
        return filtered

    async def diff_summary(self) -> str:
        if not self.ports.use_git_diff:
            return ""
        if not await self.ports._is_git_work_tree():
            return ""
        commands = [["git", "status", "--short"], ["git", "diff", "--stat"], ["git", "diff", "--name-only"]]
        parts: list[str] = []
        for command in commands:
            output = await self.ports._git_output(
                self.ports._git_command_excluding_review_private_inputs(command)
            )
            if output is not None:
                output = compat._filter_internal_git_output(
                    output,
                    command=command,
                    project_root=self.ports._active_workspace_root(),
                    task_path=self.ports._active_task_path(),
                )
                parts.append(f"$ {' '.join(command)}\n{output}")
        return "\n\n".join(parts)

    async def changed_files(self) -> list[compat.ChangedFile]:
        if not self.ports.use_git_diff:
            return self._cap_changed_files([
                changed
                for changed in compat._observed_changed_files(self.ports, limit=None)
                if not self.ports._is_review_private_path(changed.path)
            ])
        if not await self.ports._is_git_work_tree():
            return self._cap_changed_files([
                changed
                for changed in compat._observed_changed_files(self.ports, limit=None)
                if not self.ports._is_review_private_path(changed.path)
            ])
        status_text = await self.ports._git_output(
            self.ports._git_command_excluding_review_private_inputs(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
        )
        numstat_text = await self.ports._git_output(
            self.ports._git_command_excluding_review_private_inputs(
                ["git", "diff", "--numstat", "HEAD", "--"]
            )
        )
        if status_text is None and numstat_text is None:
            return self._cap_changed_files([])
        files: dict[str, compat.ChangedFile] = {}
        for path, status in compat._git_status_entries_from_porcelain_v1_z(status_text or ""):
            if (
                path
                and not self.ports._is_review_private_path(path)
                and not compat._is_ignored_changed_path(
                    path,
                    project_root=self.ports._active_workspace_root(),
                    task_path=self.ports._active_task_path(),
                )
            ):
                files[path] = compat.ChangedFile(path=path, status=status)
        for line in (numstat_text or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions = compat._parse_numstat(parts[0])
            deletions = compat._parse_numstat(parts[1])
            path = parts[2].strip()
            if " => " in path:
                path = path.rsplit(" => ", 1)[1].strip("{}")
            if (
                not path
                or self.ports._is_review_private_path(path)
                or compat._is_ignored_changed_path(
                    path,
                    project_root=self.ports._active_workspace_root(),
                    task_path=self.ports._active_task_path(),
                )
            ):
                continue
            existing = files.get(path)
            status = existing.status if existing else "modified"
            files[path] = compat.ChangedFile(path=path, status=status, additions=additions, deletions=deletions)
        observed = getattr(self.state, "observed_changed_files", None)
        if isinstance(observed, dict):
            for path, observed_file in observed.items():
                if path in files:
                    files[path].sequence = observed_file.sequence
        return self._cap_changed_files(list(files.values()))

    def _cap_changed_files(self, files: list[compat.ChangedFile]) -> list[compat.ChangedFile]:
        """Retain overflow metadata so the review packet can report its boundary."""
        return ChangedFileBatch(
            files[:compat.COMPLETION_EVIDENCE_FILE_LIMIT],
            files[compat.COMPLETION_EVIDENCE_FILE_LIMIT:],
        )

    def _record_changed_files(self, action: compat.TriggeringAction) -> None:
        if not action.paths or action.kind == "fileRead":
            return
        observed = getattr(self.state, "observed_changed_files", None)
        if observed is None:
            observed = {}
            self.state.observed_changed_files = observed
        for raw_path in action.paths:
            path = compat._workspace_display_path(self.ports._active_workspace_root(), raw_path)
            if (
                path
                and not self.ports._is_review_private_path(path)
                and not compat._is_ignored_changed_path(
                    path,
                    project_root=self.ports._active_workspace_root(),
                    task_path=self.ports._active_task_path(),
                )
            ):
                observed[path] = compat.ChangedFile(path=path, status="modified", sequence=getattr(self.ports, "_sequence", None))

    async def _is_git_work_tree(self) -> bool:
        output = await self.ports._git_output(["git", "rev-parse", "--is-inside-work-tree"])
        return output == "true"

    async def _git_output(self, command: list[str]) -> str | None:
        try:
            exec_command = list(command)
            env = None
            snapshot = getattr(self.ports, "_coder_snapshot", None)
            if snapshot is not None and command and command[0] == "git":
                if not snapshot.git_control_is_trusted():
                    return None
                exec_command = ["git", "-c", "core.fsmonitor=false", *command[1:]]
                if len(command) > 1 and command[1] == "diff":
                    exec_command = [*exec_command[:4], "--no-ext-diff", "--no-textconv", *exec_command[4:]]
                env = compat.snapshot_git_environment()
            if exec_command and exec_command[0] == "git":
                git = compat._controller_executable(
                    "git",
                    self.ports._active_workspace_root(),
                    environ=env,
                )
                if git is None:
                    return None
                exec_command[0] = git
            proc = await create_subprocess_exec(
                *exec_command,
                cwd=str(self.ports._active_workspace_root()),
                env=env,
                stdout=compat.asyncio.subprocess.PIPE,
                stderr=compat.asyncio.subprocess.PIPE,
            )
            stdout, stderr = await compat.asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                return None
            return stdout.decode("utf-8", errors="replace").strip()
        except Exception:
            return None

    async def patch_summary(self, limit: int = 4000) -> str | None:
        if not self.ports.use_git_diff:
            return None
        parts: list[str] = []
        for command in (["git", "diff", "--unified=2", "--"], ["git", "diff", "--cached", "--unified=2", "--"]):
            output = await self.ports._git_output(
                self.ports._git_command_excluding_review_private_inputs(command)
            )
            if output:
                parts.append(f"$ {' '.join(command)}\n{output}")
        if not parts:
            return None
        return compat._bounded_text("\n\n".join(parts), limit=limit)

    async def completion_packet_details(
        self,
        changed_files: list[compat.ChangedFile],
        *,
        since_sequence: int | None = None,
    ) -> dict[str, compat.Any]:
        diff_limit = 12000
        context_limit = 8000
        changed_file_diffs: list[compat.ChangedFileDiff] = []
        changed_file_contexts: list[compat.ChangedFileContext] = []
        changed_tests_summary: list[compat.ChangedTestsSummary] = []
        omitted: list[str] = []
        total_diff_chars = 0
        total_context_chars = 0
        materially_truncated = False
        truncation_reasons: list[str] = []
        is_git = self.ports.use_git_diff and await self.ports._is_git_work_tree()
        review_changed_files = [
            changed
            for changed in changed_files
            if not self.ports._is_review_private_path(changed.path)
        ]
        detail_changed_files = [
            changed
            for changed in review_changed_files
            if since_sequence is None or changed.sequence is None or changed.sequence > since_sequence
        ]
        review_validations = self.ports._review_safe_values(list(self.ports.validations))
        review_inspections = self.ports._review_safe_values(
            list(getattr(self.ports, "inspections", []))
        )
        detail_validations = [
            validation
            for validation in review_validations
            if since_sequence is None or validation.sequence > since_sequence
        ]
        detail_inspections = [
            inspection
            for inspection in review_inspections
            if since_sequence is None or inspection.sequence > since_sequence
        ]

        overflow = detail_changed_files[compat.COMPLETION_EVIDENCE_FILE_LIMIT:]
        if isinstance(changed_files, ChangedFileBatch):
            overflow = [*overflow, *changed_files.omitted]
        omitted.extend(dict.fromkeys(
            changed.path for changed in overflow
            if not self.ports._is_review_private_path(changed.path)
            and (since_sequence is None or changed.sequence is None or changed.sequence > since_sequence)
        ))
        if omitted:
            materially_truncated = True
            truncation_reasons.append(
                f"{len(omitted)} changed files exceeded the {compat.COMPLETION_EVIDENCE_FILE_LIMIT}-file evidence limit"
            )

        for changed in detail_changed_files[:compat.COMPLETION_EVIDENCE_FILE_LIMIT]:
            file_kind = compat._file_kind(changed.path)
            change_kind = compat._change_kind(changed.status)
            diff_text = ""
            omitted_reason: str | None = None
            if is_git:
                diff_text = await self.ports._changed_file_diff(changed.path)
            if not diff_text and change_kind == "added":
                file_text = compat._read_workspace_file(self.ports._active_workspace_root(), changed.path, limit=diff_limit)
                if file_text is not None:
                    diff_text = f"<new file snapshot>\n{file_text.text}"
            if not diff_text:
                omitted_reason = "No git diff or readable file snapshot was available for this changed file."
                omitted.append(changed.path)
                materially_truncated = True
            bounded_diff = compat._bounded_text(diff_text, limit=diff_limit) if diff_text else ""
            # The truncation marker can make the rendered text longer than a
            # slightly oversized input. Compare the input with the actual cap.
            diff_truncated = len(diff_text) > diff_limit
            if diff_truncated:
                materially_truncated = True
                truncation_reasons.append(f"{changed.path}: diff exceeded {diff_limit} characters")
            total_diff_chars += len(bounded_diff)
            changed_file_diffs.append(
                compat.ChangedFileDiff(
                    path=changed.path,
                    file_kind=file_kind,
                    change_kind=change_kind,
                    diff=bounded_diff,
                    diff_truncated=diff_truncated,
                    omitted_reason=omitted_reason,
                )
            )

            if change_kind == "deleted":
                continue
            context = compat._read_workspace_file(self.ports._active_workspace_root(), changed.path, limit=context_limit)
            if context is None:
                continue
            total_context_chars += len(context.text)
            if context.truncated:
                materially_truncated = True
                truncation_reasons.append(f"{changed.path}: final file context exceeded {context_limit} characters")
            changed_file_contexts.append(
                compat.ChangedFileContext(
                    path=changed.path,
                    final_snippets_around_changed_hunks=context.text,
                    context_truncated=context.truncated,
                )
            )
            if file_kind == "test":
                changed_tests_summary.append(compat._changed_tests_summary(changed.path, context.text, detail_validations))

        return {
            "changed_file_diffs": changed_file_diffs,
            "changed_file_contexts": changed_file_contexts,
            "changed_tests_summary": changed_tests_summary,
            "validation_outputs": [compat._validation_output(validation) for validation in detail_validations],
            "inspection_outputs": [compat._inspection_output(inspection) for inspection in detail_inspections],
            "completion_delta_evidence_summary": compat._completion_delta_evidence_summary(
                detail_validations,
                detail_inspections,
                since_sequence=since_sequence,
            ),
            "breadth_risk_summary": compat._breadth_risk_summary(
                task_contents=self.ports._canonical_task_text(),
                changed_files=review_changed_files,
            ),
            "diff_packet_limits": compat.DiffPacketLimits(
                total_diff_chars=total_diff_chars,
                total_context_chars=total_context_chars,
                omitted_changed_files=omitted,
                materially_truncated=materially_truncated,
                truncation_reason="; ".join(truncation_reasons) if truncation_reasons else None,
            ),
        }

    async def _changed_file_diff(self, path: str) -> str:
        if self.ports._is_review_private_path(path):
            return ""
        parts: list[str] = []
        for command in (
            ["git", "diff", "--unified=80", "--", path],
            ["git", "diff", "--cached", "--unified=80", "--", path],
        ):
            output = await self.ports._git_output(command)
            if output:
                parts.append(f"$ {' '.join(command)}\n{output}")
        return "\n\n".join(parts)
