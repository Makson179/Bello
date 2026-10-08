"""Controller evidence rules; compatibility exports live in controller."""
from __future__ import annotations

from dataclasses import dataclass

from . import compat
from .defaults import COMPLETION_EVIDENCE_FILE_LIMIT


def _latest_relevant_change_sequence(changed_files: list[compat.ChangedFile]) -> int | None:
    sequences = [
        file.sequence
        for file in changed_files
        if file.sequence is not None and compat._is_relevant_changed_path(file.path, task_contents="")
    ]
    return max(sequences) if sequences else None


def _validation_freshness_summary(
    *,
    validations: list[compat.ValidationRun],
    changed_files: list[compat.ChangedFile],
) -> str:
    latest_change = compat._latest_relevant_change_sequence(changed_files)
    passing_behavioral = [
        validation.sequence
        for validation in validations
        if compat._validation_is_usable_behavioral_pass(validation)
    ]
    last_behavioral = max(passing_behavioral) if passing_behavioral else None
    if last_behavioral is None:
        if latest_change is None:
            return "No passing behavioral validation recorded; latest relevant change sequence is unknown."
        return f"No passing behavioral validation recorded after latest relevant change sequence {latest_change}."
    if latest_change is None:
        return (
            f"Last passing behavioral validation sequence {last_behavioral}; "
            "latest relevant change sequence is unknown."
        )
    freshness = "fresh" if last_behavioral >= latest_change else "stale"
    return (
        f"Last passing behavioral validation sequence {last_behavioral}; "
        f"latest relevant change sequence {latest_change}; behavioral validation is {freshness}."
    )


def _classify_supervisor_agent_error(error: BaseException) -> str:
    if isinstance(error, compat.SupervisorTurnError):
        return "terminal_turn"
    text = str(error).lower()
    if "did not produce an agent message" in text or "no agent message" in text:
        return "no_message"
    if "rate limit" in text or "rate_limit" in text or "429" in text:
        return "rate"
    if "auth" in text or "unauthorized" in text or "forbidden" in text or "api key" in text:
        return "auth"
    if "timed out" in text or "timeout" in text:
        return "tool_timeout"
    return "unknown"


def _validation_is_fresh_behavioral_pass(validation: compat.ValidationRun, latest_change: int) -> bool:
    return compat._validation_is_usable_behavioral_pass(validation) and validation.sequence > latest_change


def _strip_test_path_extensions(name: str) -> str:
    stem = name
    suffixes = (
        ".snapshot",
        ".golden",
        ".snap",
        ".tsx",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".js",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".cs",
        ".php",
        ".vue",
        ".svelte",
        ".html",
        ".css",
        ".scss",
    )
    changed = True
    while changed:
        changed = False
        lowered = stem.lower()
        for suffix in suffixes:
            if lowered.endswith(suffix):
                stem = stem[: -len(suffix)]
                changed = True
                break
    return compat.re.sub(r"(?i)(?:^|[._-])(test|tests|spec|specs|case|cases|snapshot|snap|golden|goldens)$", "", stem)


@dataclass(frozen=True)
class _BoundedFileText:
    text: str
    truncated: bool


def _read_workspace_file(root: compat.Path, path: str, *, limit: int) -> compat._BoundedFileText | None:
    try:
        candidate = (root / path).resolve()
    except OSError:
        return None
    if not compat.ensure_relative_to(candidate, root):
        return None
    descriptor: int | None = None
    try:
        descriptor = compat._open_regular_file_no_follow(candidate)
        byte_limit = max(4, (limit + 1) * 4)
        data = bytearray()
        while len(data) < byte_limit:
            chunk = compat.os.read(descriptor, min(1024 * 1024, byte_limit - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        bytes_truncated = compat.os.fstat(descriptor).st_size > len(data)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            compat.os.close(descriptor)
    raw = bytes(data).decode("utf-8", errors="replace")
    bounded = compat._bounded_text(raw, limit=limit)
    return compat._BoundedFileText(text=bounded, truncated=bytes_truncated or len(raw) > len(bounded))


def _open_regular_file_no_follow(path: compat.Path) -> int:
    flags = compat.os.O_RDONLY | getattr(compat.os, "O_NONBLOCK", 0) | getattr(compat.os, "O_NOFOLLOW", 0)
    descriptor = compat.os.open(path, flags)
    try:
        if not compat.stat.S_ISREG(compat.os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path}")
    except BaseException:
        compat.os.close(descriptor)
        raise
    return descriptor


def _file_kind(path: str) -> str:
    lowered = path.lower().replace("\\", "/")
    name = lowered.rsplit("/", 1)[-1]
    if (
        lowered.startswith("tests/")
        or lowered.startswith("test/")
        or lowered.startswith("fixtures/")
        or lowered.startswith("fixture/")
        or lowered.startswith("golden/")
        or lowered.startswith("goldens/")
        or lowered.startswith("snapshots/")
        or lowered.startswith("__snapshots__/")
        or "/tests/" in lowered
        or "/test/" in lowered
        or "/fixtures/" in lowered
        or "/fixture/" in lowered
        or "/golden/" in lowered
        or "/goldens/" in lowered
        or "/snapshots/" in lowered
        or "/__snapshots__/" in lowered
        or "/__tests__/" in lowered
        or "/spec/" in lowered
        or ".test." in name
        or ".spec." in name
        or ".snap." in name
        or ".snapshot." in name
        or ".golden." in name
        or compat.re.search(r"(?:^|[._-])(test|tests|spec|specs|case|cases)(?:\.[^.]+)+$", name)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_spec.rb")
        or name.endswith((".snap", ".snapshot", ".golden"))
    ):
        return "test"
    if (
        lowered.startswith(".github/workflows/")
        or lowered.startswith(".circleci/")
        or lowered.startswith(".buildkite/")
        or lowered.startswith("ci/")
        or lowered.startswith(".gitlab/")
        or name in {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile"}
    ):
        return "config"
    if name in {
        "package.json",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "tsconfig.json",
        "vitest.config.js",
        "vitest.config.ts",
        "jest.config.js",
        "jest.config.ts",
        "playwright.config.js",
        "playwright.config.ts",
    }:
        return "config"
    if lowered.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        return "config"
    if lowered.endswith((".md", ".rst", ".txt", ".adoc")):
        return "docs"
    if lowered.endswith(
        (
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".mjs",
            ".cjs",
            ".rb",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".cs",
            ".php",
            ".swift",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".hpp",
            ".css",
            ".scss",
            ".html",
            ".vue",
            ".svelte",
        )
    ):
        return "source"
    return "unknown"


def _is_relevant_changed_path(path: str, *, task_contents: str) -> bool:
    if compat._is_generated_or_cache_artifact_path(path, project_root=None):
        return False
    kind = compat._file_kind(path)
    if kind in {"source", "test", "config"}:
        return True
    if compat._is_suspicious_changed_path(path):
        return True
    if kind == "docs":
        return compat._task_is_docs_facing(task_contents)
    return False


def _is_suspicious_changed_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/").strip("/")
    name = normalized.rsplit("/", 1)[-1]
    if compat._file_kind(path) == "test":
        return True
    suspicious_parts = {
        "fixtures",
        "fixture",
        "golden",
        "goldens",
        "snapshots",
        "__snapshots__",
        "__fixtures__",
        "ci",
    }
    if set(normalized.split("/")) & suspicious_parts:
        return True
    if normalized.startswith((".github/workflows/", ".circleci/", ".buildkite/")):
        return True
    if name in {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile"}:
        return True
    if any(marker in name for marker in (".snap", ".snapshot", ".golden")):
        return True
    return False


def _task_is_docs_facing(task_contents: str) -> bool:
    lowered = task_contents.lower()
    return any(token in lowered for token in ("documentation", "docs", "readme", ".md", "markdown", "docstring"))


def _read_task_text(task_path: compat.Path) -> str:
    try:
        return task_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _ensure_internal_runtime_git_excluded(project_root: compat.Path) -> None:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return
    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        current = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        entries = {line.strip() for line in current.splitlines()}
        additions = [entry for entry in (".supervisor/", ".supervisor") if entry not in entries]
        if additions:
            suffix = "" if current.endswith("\n") or not current else "\n"
            exclude_path.write_text(current + suffix + "\n".join(additions) + "\n", encoding="utf-8")
    except OSError:
        return


def _diff_line_counts(changed_files: list[compat.ChangedFile]) -> tuple[int, int]:
    additions = sum(changed.additions or 0 for changed in changed_files)
    deletions = sum(changed.deletions or 0 for changed in changed_files)
    return additions, deletions


def _breadth_risk_summary(*, task_contents: str, changed_files: list[compat.ChangedFile]) -> compat.BreadthRiskSummary:
    task_lines = [line for line in task_contents.splitlines() if line.strip()]
    lowered = task_contents.lower()
    requirement_hint_count = sum(
        1
        for line in task_lines
        if compat.re.search(
            r"\b(must|should|support|implement|handle|include|including|ensure|preserve|compatib|require|allow|prevent)\b",
            line,
            compat.re.IGNORECASE,
        )
        or compat.re.match(r"\s*[-*]\s+", line)
    )
    feature_terms = [
        term
        for term in compat.BREADTH_FEATURE_TERMS
        if compat.re.search(rf"(?<![A-Za-z0-9_]){compat.re.escape(term)}s?(?![A-Za-z0-9_])", lowered)
    ]
    additions, deletions = compat._diff_line_counts(changed_files)
    changed_source_files = [changed for changed in changed_files if compat._file_kind(changed.path) == "source"]
    changed_lines = additions + deletions
    flags: list[str] = []
    if len(task_contents) >= 2500 or len(task_lines) >= 45 or requirement_hint_count >= 10 or len(feature_terms) >= 10:
        flags.append("task_spec_appears_broad")
    if len(changed_source_files) >= 4 or changed_lines >= compat.LARGE_DIFF_CHANGED_LINES_THRESHOLD:
        flags.append("implementation_diff_is_broad")
    if len(feature_terms) >= 8:
        flags.append("many_task_feature_terms")
    suggested_min = 0
    if flags:
        suggested_min = 6
        if len(task_contents) >= 6000 or requirement_hint_count >= 18 or len(feature_terms) >= 16:
            suggested_min = 8
    return compat.BreadthRiskSummary(
        flags=flags,
        task_line_count=len(task_lines),
        requirement_hint_count=requirement_hint_count,
        task_feature_terms=feature_terms,
        changed_source_files_count=len(changed_source_files),
        changed_lines=changed_lines,
        suggested_min_behavior_rows=suggested_min,
    )


def _has_large_diff(changed_files: list[compat.ChangedFile]) -> bool:
    additions, deletions = compat._diff_line_counts(changed_files)
    return (
        len(changed_files) >= compat.LARGE_DIFF_CHANGED_FILES_THRESHOLD
        or additions + deletions >= compat.LARGE_DIFF_CHANGED_LINES_THRESHOLD
    )


def _change_kind(status: str) -> str:
    normalized = status.strip().upper()
    if "D" in normalized:
        return "deleted"
    if "R" in normalized:
        return "renamed"
    if "A" in normalized or "?" in normalized:
        return "added"
    if normalized:
        return "modified"
    return "unknown"


def _changed_tests_summary(path: str, text: str, validations: list[compat.ValidationRun]) -> compat.ChangedTestsSummary:
    return compat.ChangedTestsSummary(
        path=path,
        added_or_modified_test_names=compat._detect_test_names(text),
        changed_assertion_snippets=compat._assertion_snippets(text),
        grep_or_test_selection_relevant_to_validations=[
            validation.command
            for validation in validations
            if path in compat._target_files_or_test_files(validation.command)
        ],
        summary_truncated=text.endswith("...<truncated>"),
    )


def _validation_output(validation: compat.ValidationRun) -> compat.ValidationOutput:
    return compat.ValidationOutput(
        validation_id=validation.validation_id,
        command=validation.command,
        raw_command=validation.raw_command,
        normalized_command=validation.normalized_command,
        cwd=validation.cwd,
        exit_code=validation.exit_code,
        shell_exit_code=validation.shell_exit_code,
        type=validation.type,
        outcome=validation.outcome,
        passed=validation.passed,
        trusted_validation_outcome=validation.trusted_validation_outcome,
        masking_reason=validation.masking_reason,
        sequence=validation.sequence,
        stdout_or_summary=validation.summary,
        stderr_or_summary=None,
        captured_output=validation.captured_output,
        output_truncated=validation.summary.endswith("...<truncated>") or validation.captured_output_truncated,
        detected_test_names=compat._detect_test_names(validation.summary),
        target_files_or_test_files=validation.target_files_or_test_files
        or compat._target_files_or_test_files(validation.command),
        was_filtered=validation.was_filtered,
        raw_selector=validation.raw_selector,
        executed_test_names=validation.executed_test_names,
        executed_test_files=validation.executed_test_files,
        passed_count=validation.passed_count,
        failed_count=validation.failed_count,
    )


def _completion_delta_evidence_summary(
    validations: list[compat.ValidationRun],
    inspections: list[compat.InspectionRun],
    *,
    since_sequence: int | None,
) -> list[str]:
    if since_sequence is None:
        return []
    items: list[str] = []
    for validation in validations:
        items.append(
            (
                f"validation {validation.validation_id} seq={validation.sequence} "
                f"type={validation.type} outcome={validation.trusted_validation_outcome} "
                f"command={compat._bounded_text(validation.command, limit=160)}"
            )
        )
    for inspection in inspections:
        outcome = "passed" if inspection.passed and inspection.outcome == "pass" else "failed"
        items.append(
            (
                f"inspection {inspection.inspection_id} seq={inspection.sequence} "
                f"outcome={outcome} command={compat._bounded_text(inspection.command, limit=160)}"
            )
        )
    if not items:
        return [f"No validation or inspection records after return baseline sequence {since_sequence}."]
    return items[:30]


def _inspection_output(inspection: compat.InspectionRun) -> compat.InspectionOutput:
    return compat.InspectionOutput(
        inspection_id=inspection.inspection_id,
        command=inspection.command,
        raw_command=inspection.raw_command,
        normalized_command=inspection.normalized_command,
        cwd=inspection.cwd,
        exit_code=inspection.exit_code,
        shell_exit_code=inspection.shell_exit_code,
        outcome=inspection.outcome,
        passed=inspection.passed,
        sequence=inspection.sequence,
        stdout_or_summary=inspection.summary,
        captured_output=inspection.captured_output,
        output_truncated=inspection.summary.endswith("...<truncated>") or inspection.captured_output_truncated,
        inspected_paths=inspection.inspected_paths,
    )


def _evidence_provenance_summary(
    *,
    validations: list[compat.ValidationRun],
    changed_files: list[compat.ChangedFile],
    latest_change_sequence: int | None,
) -> compat.EvidenceProvenanceSummary:
    changed_test_files = compat._changed_test_files(changed_files)
    return compat.EvidenceProvenanceSummary(
        latest_relevant_change_sequence=latest_change_sequence,
        changed_test_files=changed_test_files,
        validations=[
            compat._validation_provenance(
                validation,
                changed_test_files=changed_test_files,
                latest_change_sequence=latest_change_sequence,
            )
            for validation in validations[-compat.VALIDATION_LEDGER_LIMIT:]
        ],
    )


def _changed_test_files(changed_files: list[compat.ChangedFile]) -> list[str]:
    files = [
        compat._normalize_review_path(changed.path)
        for changed in changed_files
        if compat._file_kind(changed.path) == "test"
    ]
    return list(dict.fromkeys(path for path in files if path))


def _changed_test_file_identity_map(changed_test_files: list[str]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for path in changed_test_files:
        identity = compat._canonical_test_file_identity(path)
        if identity and identity not in identities:
            identities[identity] = path
    return identities


def _partition_executed_test_files(
    executed_files: list[str],
    *,
    changed_test_identities: dict[str, str],
) -> tuple[list[str], list[str]]:
    coder_authored_files: list[str] = []
    untouched_files: list[str] = []
    for path in executed_files:
        identity = compat._canonical_test_file_identity(path)
        changed_path = changed_test_identities.get(identity)
        if changed_path:
            coder_authored_files.append(changed_path)
        else:
            untouched_files.append(path)
    return list(dict.fromkeys(coder_authored_files)), list(dict.fromkeys(untouched_files))


def _canonical_test_file_identity(path: str) -> str:
    normalized = compat._normalize_review_path(path)
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return ""
    name = parts[-1]
    stem = compat._strip_test_path_extensions(name)
    if not stem:
        stem = name
    prefix = "/".join(parts[:-1])
    identity = f"{prefix}/{stem}" if prefix else stem
    return identity.lower()


def _validation_provenance(
    validation: compat.ValidationRun,
    *,
    changed_test_files: list[str],
    latest_change_sequence: int | None,
) -> compat.ValidationProvenance:
    executed_files = list(dict.fromkeys(compat._normalize_review_path(path) for path in validation.executed_test_files if path))
    coder_authored_files, untouched_files = compat._partition_executed_test_files(
        executed_files,
        changed_test_identities=compat._changed_test_file_identity_map(changed_test_files),
    )
    captured_output = validation.captured_output or ""
    captured_output_present = bool(captured_output.strip())
    fresh = None if latest_change_sequence is None else validation.sequence > latest_change_sequence
    output_kind = compat._validation_output_kind(validation, captured_output_present=captured_output_present)
    independence_class, risk_reasons = compat._validation_independence(
        validation,
        fresh_after_latest_relevant_change=fresh,
        captured_output_present=captured_output_present,
        output_kind=output_kind,
        executed_test_files=executed_files,
        coder_authored_test_files=coder_authored_files,
        untouched_executed_test_files=untouched_files,
    )
    return compat.ValidationProvenance(
        validation_id=validation.validation_id,
        command=validation.command,
        type=validation.type,
        passed=validation.outcome == "pass" and validation.passed,
        trusted_validation_outcome=validation.trusted_validation_outcome,
        sequence=validation.sequence,
        fresh_after_latest_relevant_change=fresh,
        captured_output_present=captured_output_present,
        output_identifies_test_files=bool(executed_files),
        executed_test_files=executed_files,
        coder_authored_test_files=coder_authored_files,
        untouched_executed_test_files=untouched_files,
        target_files_or_test_files=validation.target_files_or_test_files
        or compat._target_files_or_test_files(validation.command),
        output_kind=output_kind,
        independence_class=independence_class,
        risk_reasons=risk_reasons,
    )


def _validation_output_kind(
    validation: compat.ValidationRun,
    *,
    captured_output_present: bool,
) -> str:
    if validation.type == "static":
        return "not_applicable"
    if not captured_output_present:
        return "missing"
    if validation.type == "behavioral":
        if validation.executed_test_files or validation.passed_count is not None or validation.failed_count is not None:
            return "test_runner_output"
        return "unknown"
    if validation.type == "behavior_demo":
        if compat._captured_output_looks_like_test_runner(validation.captured_output):
            return "test_runner_output"
        if compat._captured_output_is_self_verdict_only(validation.captured_output):
            return "self_verdict_only"
        return "factual_observation_candidate"
    return "unknown"


def _validation_independence(
    validation: compat.ValidationRun,
    *,
    fresh_after_latest_relevant_change: bool | None,
    captured_output_present: bool,
    output_kind: str,
    executed_test_files: list[str],
    coder_authored_test_files: list[str],
    untouched_executed_test_files: list[str],
) -> tuple[str, list[str]]:
    risk_reasons: list[str] = []
    if validation.trusted_validation_outcome == "masked_or_unknown":
        risk_reasons.append(validation.masking_reason or "masked_or_unknown_validation")
        return "masked_or_unknown", risk_reasons
    if validation.outcome != "pass" or not validation.passed or validation.trusted_validation_outcome != "passed":
        risk_reasons.append("failed_validation")
        return "failed", risk_reasons
    if fresh_after_latest_relevant_change is False:
        risk_reasons.append("stale_after_latest_relevant_change")
        return "stale", risk_reasons
    if validation.type == "static":
        risk_reasons.append("static_validation_not_behavioral_evidence")
        return "not_independent", risk_reasons
    if validation.type == "behavior_demo":
        if not captured_output_present:
            risk_reasons.append("behavior_demo_missing_captured_output")
            return "not_independent", risk_reasons
        if output_kind == "self_verdict_only":
            risk_reasons.append("behavior_demo_self_verdict_only")
            return "not_independent", risk_reasons
        if output_kind == "test_runner_output":
            risk_reasons.append("behavior_demo_looks_like_test_runner_output")
            return "not_independent", risk_reasons
        return "independent_candidate", risk_reasons
    if validation.type == "behavioral":
        if not executed_test_files:
            risk_reasons.append("unknown_test_file_provenance")
            return "unknown", risk_reasons
        if untouched_executed_test_files:
            return "independent", risk_reasons
        if coder_authored_test_files and len(coder_authored_test_files) == len(executed_test_files):
            risk_reasons.append("all_output_identified_tests_were_coder_authored")
            return "self_confirming", risk_reasons
        risk_reasons.append("unknown_test_file_provenance")
        return "unknown", risk_reasons
    return "unknown", risk_reasons


def _captured_output_is_self_verdict_only(output: str) -> bool:
    lines = [line.strip().strip(".!").lower() for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    verdict_pattern = compat.re.compile(
        r"^(?:pass(?:ed)?|ok|success(?:ful)?|works?|correct|done|green|valid|all good)$"
    )
    return all(verdict_pattern.fullmatch(line) for line in lines)


def _captured_output_looks_like_test_runner(output: str) -> bool:
    text = output.strip()
    if not text:
        return False
    patterns = (
        r"(?m)^\s*(?:PASS|FAIL)\s+[\w@+./-]+",
        r"(?m)\b[\w@+./-]+::test_[\w.\[\]-]+\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS)\b",
        r"(?i)\b\d+\s+(?:passed|passing|failed|failing|skipped)\b",
        r"(?i)\btest result:\s+(?:ok|failed)\b",
    )
    return any(compat.re.search(pattern, text) for pattern in patterns)


def _detect_test_names(text: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    patterns = (
        r"\b(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"\bdef\s+(test_[A-Za-z0-9_]+)\s*\(",
        r"\bclass\s+(Test[A-Za-z0-9_]+)\b",
    )
    for pattern in patterns:
        for match in compat.re.finditer(pattern, text):
            names.append(match.group(1).strip())
            if len(names) >= limit:
                return list(dict.fromkeys(names))
    return list(dict.fromkeys(names))


def _assertion_snippets(text: str, *, limit: int = 30) -> list[str]:
    snippets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if any(token in lowered for token in ("assert", "expect(", ".should", "equal", "strictEqual".lower())):
            snippets.append(compat._bounded_text(stripped, limit=240))
            if len(snippets) >= limit:
                break
    return snippets


def _patch_summary_from_item(item: compat.Any, limit: int = 4000) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "fileChange":
        return None
    changes = item.get("changes") or item.get("fileChanges")
    if changes is None:
        return None
    return compat._bounded_json(changes, limit=limit)


def _patch_summary_from_approval_context(context: compat.ApprovalContext, limit: int = 4000) -> str | None:
    if context.diff:
        return compat._bounded_text(context.diff, limit=limit)
    if context.file_changes:
        return compat._bounded_json(context.file_changes, limit=limit)
    return None


def _bounded_text(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...<truncated>"


def _bounded_json(value: compat.Any, *, limit: int) -> str:
    text = compat.json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return compat._bounded_text(text, limit=limit)


def _parse_numstat(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _observed_changed_files(
    controller: compat.Any,
    *,
    limit: int | None = COMPLETION_EVIDENCE_FILE_LIMIT,
) -> list[compat.ChangedFile]:
    observed = getattr(controller, "observed_changed_files", None)
    if not isinstance(observed, dict):
        return []
    project_root = getattr(controller, "project_root", None)
    task_path = getattr(controller, "task_path", None)
    return [
        changed
        for changed in observed.values()
        if not compat._is_ignored_changed_path(changed.path, project_root=project_root, task_path=task_path)
    ][:limit]


def _path_from_git_status_line(line: str) -> str:
    if len(line) > 2 and line[2] == " ":
        return line[3:].strip()
    if len(line) > 2:
        return line[2:].strip()
    return line.strip()


def _git_status_entries_from_porcelain_v1_z(output: str) -> list[tuple[str, str]]:
    records = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4 or record[2] != " ":
            continue
        raw_status = record[:2]
        path = record[3:]
        if path:
            entries.append((path, raw_status.strip() or "modified"))
        if "R" in raw_status or "C" in raw_status:
            # In -z mode Git emits the destination in this record and the
            # source path as the following NUL-delimited record.
            index += 1
    return entries


def _format_validation(validation: compat.ValidationRun) -> str:
    exit_code = "unknown" if validation.exit_code is None else str(validation.exit_code)
    return f"{validation.command} ({validation.type} {validation.outcome}, exit={exit_code})"


def _workspace_display_path(project_root: compat.Path, raw_path: str) -> str:
    path = compat.Path(raw_path)
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return raw_path


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _is_internal_runtime_path(path: str, *, project_root: compat.Path | None, task_path: compat.Path | str | None) -> bool:
    normalized = compat._normalize_internal_workspace_path(str(path).strip().strip("'\""))
    if not normalized:
        return False
    if normalized == ".git-init.log":
        return True
    if normalized == ".supervisor" or normalized.startswith(".supervisor/"):
        return True
    task_relative = compat._task_relative_workspace_path(project_root=project_root, task_path=task_path)
    return bool(task_relative and normalized == task_relative)


def _is_ignored_changed_path(path: str, *, project_root: compat.Path | None, task_path: compat.Path | str | None) -> bool:
    return compat._is_internal_runtime_path(
        path,
        project_root=project_root,
        task_path=task_path,
    ) or compat._is_generated_or_cache_artifact_path(path, project_root=project_root)


def _is_generated_or_cache_artifact_path(path: str, *, project_root: compat.Path | None) -> bool:
    normalized = compat._normalize_internal_workspace_path(str(path).strip().strip("'\""))
    if not normalized:
        return False
    parts = set(normalized.lower().split("/"))
    if parts & {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".parcel-cache",
        "node_modules",
    }:
        return True
    name = normalized.rsplit("/", 1)[-1].lower()
    if name.endswith(
        (
            ".pyc",
            ".pyo",
            ".gcda",
            ".gcno",
            ".tsbuildinfo",
        )
    ):
        return True
    return False


def _task_relative_workspace_path(*, project_root: compat.Path | None, task_path: compat.Path | str | None) -> str | None:
    if task_path is None:
        return None
    task = compat.Path(task_path)
    if project_root is not None:
        try:
            task = task.resolve()
            return compat._normalize_internal_workspace_path(str(task.relative_to(compat.Path(project_root).resolve())))
        except (OSError, ValueError):
            pass
    if task.is_absolute():
        return compat._normalize_internal_workspace_path(task.name)
    return compat._normalize_internal_workspace_path(str(task))


def _normalize_internal_workspace_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _filter_internal_git_output(
    output: str,
    *,
    command: list[str],
    project_root: compat.Path,
    task_path: compat.Path,
) -> str:
    if not output:
        return output
    if command[:2] == ["git", "status"]:
        lines = [
            line
            for line in output.splitlines()
            if not compat._is_ignored_changed_path(
                compat._git_status_changed_path(line),
                project_root=project_root,
                task_path=task_path,
            )
        ]
        return "\n".join(lines)
    if command[:2] == ["git", "diff"] and "--name-only" in command:
        lines = [
            line
            for line in output.splitlines()
            if not compat._is_ignored_changed_path(line.strip(), project_root=project_root, task_path=task_path)
        ]
        return "\n".join(lines)
    if command[:2] == ["git", "diff"] and "--stat" in command:
        lines: list[str] = []
        for line in output.splitlines():
            if "|" not in line:
                continue
            path = line.split("|", 1)[0].strip()
            if not compat._is_ignored_changed_path(path, project_root=project_root, task_path=task_path):
                lines.append(line)
        return "\n".join(lines)
    return output


def _git_status_changed_path(line: str) -> str:
    path = compat._path_from_git_status_line(line)
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1].strip()
    return path


def _changed_files_from_diff_summary(
    diff: str | None,
    *,
    project_root: compat.Path | None = None,
    task_path: compat.Path | str | None = None,
) -> list[str]:
    if not diff:
        return []
    files: list[str] = []
    status_marker = "$ git status --short"
    if status_marker in diff:
        status_tail = diff.split(status_marker, 1)[1].split("$ git diff --stat", 1)[0]
        for line in status_tail.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("$"):
                continue
            path = compat._git_status_changed_path(stripped)
            if path and not compat._is_ignored_changed_path(path, project_root=project_root, task_path=task_path) and path not in files:
                files.append(path)
    marker = "$ git diff --name-only"
    if marker in diff:
        tail = diff.split(marker, 1)[1]
        for line in tail.splitlines():
            path = line.strip()
            if (
                path
                and not path.startswith("$")
                and not compat._is_ignored_changed_path(path, project_root=project_root, task_path=task_path)
                and path not in files
            ):
                files.append(path)
    return files
