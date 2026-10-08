"""Isolated snapshot Git metadata and private-input visibility.

Owns Git invocation isolation, baseline/config setup and restoration, review
metadata copying, and removal of controller-private material from snapshot Git."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
    )


def _scrub_private_plan_from_snapshot_git(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> None:
    """Remove any coder-created Git reference to the private plan before revision.

    The ordinary ignore rule prevents routine staging, but a coder can explicitly use
    ``git add -f``.  Completion receives a fresh reachable-object clone and is already
    protected; a revision coder reuses this snapshot, so its index, reflogs, and loose
    objects must be scrubbed before that fresh thread starts.
    """

    relative = snapshot.plan_relative_path
    if relative is None:
        return
    ops._restore_trusted_snapshot_git_config(snapshot)
    git = ops._git_executable(snapshot.snapshot_root)

    def run(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [git, *arguments],
            cwd=snapshot.snapshot_root,
            env=ops._isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    history = run(
        [
            "--literal-pathspecs",
            "log",
            "--all",
            "--format=%H",
            "--",
            relative,
        ]
    )
    if history.returncode != 0:
        detail = history.stderr.decode("utf-8", errors="replace").strip()
        raise ops.WorkspaceSnapshotError(
            "failed to inspect coder snapshot history before detaching the private plan"
            + (f": {detail}" if detail else "")
        )
    if history.stdout.strip():
        raise ops.WorkspaceSnapshotError(
            "coder snapshot Git history contains the private plan and cannot be exposed "
            f"to the revision coder: {relative}"
        )

    removal = run(
        [
            "--literal-pathspecs",
            "update-index",
            "--force-remove",
            "--",
            relative,
        ]
    )
    if removal.returncode != 0:
        detail = removal.stderr.decode("utf-8", errors="replace").strip()
        raise ops.WorkspaceSnapshotError(
            "failed to remove the private plan from the coder snapshot Git index"
            + (f": {detail}" if detail else "")
        )

    for arguments, label in (
        (["reflog", "expire", "--expire=now", "--all"], "expire snapshot Git reflogs"),
        (["prune", "--expire=now"], "prune private snapshot Git objects"),
    ):
        completed = run(arguments)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ops.WorkspaceSnapshotError(
                f"failed to {label}" + (f": {detail}" if detail else "")
            )

    tracked = run(
        [
            "--literal-pathspecs",
            "ls-files",
            "--error-unmatch",
            "--",
            relative,
        ]
    )
    if tracked.returncode == 0:
        raise ops.WorkspaceSnapshotError(
            f"coder snapshot Git index still exposes the private plan: {relative}"
        )
    if tracked.returncode != 1:
        detail = tracked.stderr.decode("utf-8", errors="replace").strip()
        raise ops.WorkspaceSnapshotError(
            "failed to verify private-plan removal from the coder snapshot Git index"
            + (f": {detail}" if detail else "")
        )


def _gitignore_literal_path(ops: SnapshotServices, /, raw_path: str) -> str:
    # `.git/info/exclude` uses gitignore syntax. Escape every metacharacter while
    # retaining slash separators so the post-baseline runtime mount stays quiet in
    # ordinary `git status` output without becoming part of Git history.
    return re.sub(r"([\\ *?!\[\]#])", r"\\\1", raw_path)


def _init_snapshot_git(ops: SnapshotServices, /, snapshot_root: Path) -> str:
    identity = [
        "-c",
        "user.email=bello@localhost",
        "-c",
        "user.name=Bello Snapshot",
        "-c",
        "commit.gpgsign=false",
    ]
    if not (snapshot_root / ".git").exists():
        ops._run_git(snapshot_root, ["init", "-q"])
    ops._run_git(snapshot_root, ["config", "--local", "core.hooksPath", os.devnull])
    ops._run_git(snapshot_root, ["config", "--local", "commit.gpgsign", "false"])
    ops._run_git(snapshot_root, ["config", "--local", "tag.gpgsign", "false"])
    ops._run_git(snapshot_root, ["config", "--local", "user.email", "bello@localhost"])
    ops._run_git(snapshot_root, ["config", "--local", "user.name", "Bello Snapshot"])
    ops._run_git(snapshot_root, ["add", "-f", "-A", "--"])
    ops._run_git(
        snapshot_root,
        [
            *identity,
            "commit",
            "-q",
            "--no-verify",
            "--allow-empty",
            "-m",
            "bello coder snapshot baseline",
        ],
    )
    baseline_commit = str(ops._run_git(snapshot_root, ["rev-parse", "HEAD"])).strip()
    ops._run_git(snapshot_root, ["update-ref", "refs/bello/baseline", baseline_commit])
    return baseline_commit


def _restore_trusted_snapshot_git_config(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> None:
    git_dir = snapshot.snapshot_root / ".git"
    if ops.is_link_or_reparse(git_dir) or not git_dir.is_dir():
        raise ops.SnapshotPatchError("snapshot Git directory was replaced or removed")
    ops._atomic_replace_bytes(
        git_dir / "config", snapshot.git_config_bytes, snapshot.git_config_mode
    )
    worktree_config = git_dir / "config.worktree"
    if snapshot.git_worktree_config_bytes is None:
        ops._remove_path(worktree_config)
    else:
        ops._atomic_replace_bytes(
            worktree_config,
            snapshot.git_worktree_config_bytes,
            snapshot.git_worktree_config_mode or 0o644,
        )


def _clone_git_metadata(
    ops: SnapshotServices,
    /,
    original_root: Path,
    snapshot_root: Path,
    *,
    fail_on_clone_error: bool = False,
) -> bool:
    probe = subprocess.run(
        [ops._git_executable(original_root), "rev-parse", "--show-toplevel"],
        cwd=original_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        return False
    try:
        top_level = Path(probe.stdout.strip()).resolve()
    except OSError:
        return False
    if top_level != original_root:
        return False
    cloned = subprocess.run(
        [
            ops._git_executable(original_root),
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            str(original_root),
            str(snapshot_root),
        ],
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if cloned.returncode == 0:
        return True
    ops._cleanup_path_best_effort(snapshot_root)
    if fail_on_clone_error:
        detail = cloned.stderr.decode("utf-8", errors="replace").strip()
        raise ops.WorkspaceSnapshotError(
            "failed to clone Git metadata for verification snapshot"
            + (f": {detail}" if detail else "")
        )
    return False


def _is_top_level_git_repository(ops: SnapshotServices, /, root: Path) -> bool:
    probe = subprocess.run(
        [ops._git_executable(root), "rev-parse", "--show-toplevel"],
        cwd=root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        return False
    try:
        return Path(probe.stdout.strip()).resolve() == root.resolve()
    except OSError:
        return False


def _verification_gitlink_paths(
    ops: SnapshotServices, /, root: Path
) -> tuple[str, ...]:
    probe = subprocess.run(
        [ops._git_executable(root), "ls-files", "--stage", "-z"],
        cwd=root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if probe.returncode != 0:
        return ()
    paths: list[str] = []
    for record in probe.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if separator and metadata.startswith(b"160000 "):
            paths.append(raw_path.decode("utf-8", errors="surrogateescape"))
    return tuple(paths)


def _git_metadata_path(
    ops: SnapshotServices, /, root: Path, relative: str, *, required: bool
) -> Path | None:
    raw = str(ops._run_git(root, ["rev-parse", "--git-path", relative])).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    common_raw = str(ops._run_git(root, ["rev-parse", "--git-common-dir"])).strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = root / common
    if not path.exists() and not path.is_symlink():
        if required:
            raise ops.WorkspaceSnapshotError(
                f"required Git metadata file is missing: {relative}"
            )
        return None
    if ops.is_link_or_reparse(path) or not path.is_file():
        raise ops.WorkspaceSnapshotError(
            f"Git metadata file is not a regular file: {relative}"
        )
    try:
        common_resolved = common.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(common_resolved)
    except (OSError, ValueError) as exc:
        raise ops.WorkspaceSnapshotError(
            f"Git metadata path escapes the repository common directory: {relative}"
        ) from exc
    return resolved


def _copy_verification_git_file(
    ops: SnapshotServices,
    /,
    original_root: Path,
    snapshot_root: Path,
    relative: str,
    *,
    required: bool = False,
) -> None:
    source = ops._git_metadata_path(original_root, relative, required=required)
    target_raw = str(
        ops._run_git(snapshot_root, ["rev-parse", "--git-path", relative])
    ).strip()
    target = Path(target_raw)
    if not target.is_absolute():
        target = snapshot_root / target
    if source is None:
        ops._remove_path(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    ops._remove_path(target)
    shutil.copy2(source, target, follow_symlinks=False)


def _git_config_file_values(
    ops: SnapshotServices, /, path: Path, key: str
) -> list[str]:
    completed = subprocess.run(
        [
            ops._git_executable(path.parent),
            "config",
            "--file",
            str(path),
            "--get-all",
            key,
        ],
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise ops.WorkspaceSnapshotError(
            f"failed to read safe Git config key {key}: {completed.stderr.strip()}"
        )
    return completed.stdout.splitlines() if completed.returncode == 0 else []


def _git_config_has_include(ops: SnapshotServices, /, path: Path) -> bool:
    completed = subprocess.run(
        [
            ops._git_executable(path.parent),
            "config",
            "--file",
            str(path),
            "--name-only",
            "--get-regexp",
            r"^include(if)?\..*",
        ],
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise ops.WorkspaceSnapshotError(
            f"failed to inspect Git config includes: {completed.stderr.strip()}"
        )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _copy_verification_safe_git_config(
    ops: SnapshotServices, /, original_root: Path, snapshot_root: Path
) -> None:
    config_files: list[Path] = []
    for relative in ("config", "config.worktree"):
        path = ops._git_metadata_path(
            original_root, relative, required=relative == "config"
        )
        if path is not None:
            if ops._git_config_has_include(path):
                raise ops.WorkspaceSnapshotError(
                    "verification snapshot refuses repository-local Git config includes"
                )
            config_files.append(path)
    for key, allowed in ops.VERIFICATION_SAFE_GIT_CONFIG.items():
        values: list[str] = []
        for config_file in config_files:
            values.extend(ops._git_config_file_values(config_file, key))
        if not values:
            continue
        value = values[-1].strip().lower()
        if allowed is not None and value not in allowed:
            raise ops.WorkspaceSnapshotError(
                f"unsupported value for safe Git config key {key}: {value}"
            )
        ops._run_git(snapshot_root, ["config", "--local", key, value])


def _git_config_bool(ops: SnapshotServices, /, root: Path, key: str) -> bool:
    values = ops._optional_git_lines(
        root, ["config", "--local", "--bool", "--get", key]
    )
    return bool(values and values[-1].strip().lower() == "true")


def _reject_verification_git_alternates(ops: SnapshotServices, /, root: Path) -> None:
    alternates = ops._git_metadata_path(root, "objects/info/alternates", required=False)
    if alternates is None:
        return
    if alternates.stat().st_size > 0:
        raise ops.WorkspaceSnapshotError(
            "verification snapshot refuses external Git object alternates"
        )


def _copy_snapshot_git_index(
    ops: SnapshotServices, /, original_root: Path, snapshot_root: Path
) -> None:
    source = ops._git_metadata_path(original_root, "index", required=False)
    target_raw = str(
        ops._run_git(snapshot_root, ["rev-parse", "--git-path", "index"])
    ).strip()
    target = Path(target_raw)
    if not target.is_absolute():
        target = snapshot_root / target
    if source is None:
        # An unborn or empty repository may legitimately have no index yet.  The cloned
        # repository is still useful for inspection, and copied files remain visible as
        # untracked state.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    # A split index contains only a delta and refers to a sibling sharedindex.<hash> file.
    # Local clone does not reliably carry that unreferenced file, so copy exactly the
    # referenced shared index before asking Git to materialize a standalone target index.
    shared_raw = str(
        ops._run_git(original_root, ["rev-parse", "--shared-index-path"])
    ).strip()
    if shared_raw:
        shared_index = Path(shared_raw)
        if not shared_index.is_absolute():
            shared_index = original_root / shared_index
        if (
            shared_index.is_symlink()
            or not shared_index.is_file()
            or shared_index.parent.resolve() != source.parent.resolve()
            or re.fullmatch(r"sharedindex\.[0-9a-fA-F]{40,64}", shared_index.name)
            is None
        ):
            raise ops.WorkspaceSnapshotError(
                "verification snapshot source shared index is not a regular file"
            )
        shutil.copy2(
            shared_index,
            target.parent / shared_index.name,
            follow_symlinks=False,
        )
    ops._run_git(snapshot_root, ["update-index", "--no-split-index"])
    ops._run_git(snapshot_root, ["ls-files", "--stage", "-z"])


def _hide_verification_runtime_state(
    ops: SnapshotServices, /, snapshot_root: Path
) -> None:
    tracked = subprocess.run(
        [
            ops._git_executable(snapshot_root),
            "ls-files",
            "--error-unmatch",
            "--",
            ".supervisor",
        ],
        cwd=snapshot_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tracked.returncode == 0:
        raise ops.WorkspaceSnapshotError(
            "verification source unexpectedly tracks private .supervisor runtime state"
        )
    history = subprocess.run(
        [
            ops._git_executable(snapshot_root),
            "log",
            "--all",
            "--format=%H",
            "--",
            ".supervisor",
        ],
        cwd=snapshot_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if history.returncode != 0 or history.stdout.strip():
        raise ops.WorkspaceSnapshotError(
            "verification source Git history exposes private .supervisor runtime state"
        )


def _hide_verification_private_inputs(
    ops: SnapshotServices,
    /,
    snapshot_root: Path,
    private_runtime_paths: tuple[str, ...],
) -> None:
    if not private_runtime_paths:
        return
    ops._remove_private_plan_git_exclude(snapshot_root)
    for relative in private_runtime_paths:
        history = subprocess.run(
            [
                ops._git_executable(snapshot_root),
                "--literal-pathspecs",
                "log",
                "--all",
                "--format=%H",
                "--",
                relative,
            ],
            cwd=snapshot_root,
            env=ops._isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if history.returncode != 0 or history.stdout.strip():
            raise ops.WorkspaceSnapshotError(
                "verification source Git history exposes private plan input: "
                f"{relative}"
            )
        removal = subprocess.run(
            [
                ops._git_executable(snapshot_root),
                "--literal-pathspecs",
                "update-index",
                "--force-remove",
                "--",
                relative,
            ],
            cwd=snapshot_root,
            env=ops._isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if removal.returncode != 0:
            detail = removal.stderr.decode("utf-8", errors="replace").strip()
            raise ops.WorkspaceSnapshotError(
                "failed to remove private plan input from verification Git index"
                + (f": {detail}" if detail else "")
            )
        tracked = subprocess.run(
            [
                ops._git_executable(snapshot_root),
                "--literal-pathspecs",
                "ls-files",
                "--error-unmatch",
                "--",
                relative,
            ],
            cwd=snapshot_root,
            env=ops._isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if tracked.returncode == 0:
            raise ops.WorkspaceSnapshotError(
                f"verification Git index still exposes private plan input: {relative}"
            )
        if tracked.returncode != 1:
            detail = tracked.stderr.decode("utf-8", errors="replace").strip()
            raise ops.WorkspaceSnapshotError(
                "failed to verify private plan removal from the verification Git index"
                + (f": {detail}" if detail else "")
            )


def _remove_private_plan_git_exclude(
    ops: SnapshotServices, /, snapshot_root: Path
) -> None:
    exclude = snapshot_root / ".git" / "info" / "exclude"
    if not (exclude.exists() or exclude.is_symlink()):
        return
    raw, mode = ops._read_regular_file(exclude)
    text = raw.decode("utf-8", errors="surrogateescape")
    kept: list[str] = []
    in_private_block = False
    for line in text.splitlines(keepends=True):
        value = line.rstrip("\r\n")
        if value == ops.PRIVATE_PLAN_EXCLUDE_BEGIN:
            in_private_block = True
            continue
        if value == ops.PRIVATE_PLAN_EXCLUDE_END:
            in_private_block = False
            continue
        if in_private_block:
            continue
        kept.append(line)
    if in_private_block:
        raise ops.WorkspaceSnapshotError(
            "verification source has an unterminated private plan Git exclusion"
        )
    ops._atomic_replace_bytes(
        exclude,
        "".join(kept).encode("utf-8", errors="surrogateescape"),
        mode,
    )


def _sanitize_verification_snapshot_git(
    ops: SnapshotServices, /, snapshot_root: Path
) -> None:
    git_dir = snapshot_root / ".git"
    if ops.is_link_or_reparse(git_dir) or not git_dir.is_dir():
        raise ops.WorkspaceSnapshotError(
            "verification snapshot Git directory is not a regular directory"
        )
    hooks = git_dir / "hooks"
    ops._remove_path(hooks)
    hooks.mkdir(mode=0o700)
    ops._remove_path(git_dir / "objects" / "info" / "alternates")
    for key, value in (
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        ops._run_git(snapshot_root, ["config", "--local", key, value])
    # A local clone adds an origin pointing at the submitted workspace.  Completion has
    # no need for it, and removing it prevents a review command from addressing the
    # candidate through a Git remote even though network access is disabled.
    for name in ops._optional_git_lines(snapshot_root, ["remote"]):
        ops._run_git(snapshot_root, ["remote", "remove", name])


def _sync_snapshot_remotes(
    ops: SnapshotServices, /, original_root: Path, snapshot_root: Path
) -> None:
    for name in ops._optional_git_lines(snapshot_root, ["remote"]):
        ops._run_git(snapshot_root, ["remote", "remove", name])
    for name in ops._optional_git_lines(original_root, ["remote"]):
        fetch_urls = ops._optional_git_lines(
            original_root, ["remote", "get-url", "--all", name]
        )
        if not fetch_urls:
            continue
        ops._run_git(snapshot_root, ["remote", "add", name, fetch_urls[0]])
        for url in fetch_urls[1:]:
            ops._run_git(snapshot_root, ["remote", "set-url", "--add", name, url])
        push_urls = ops._optional_git_lines(
            original_root, ["remote", "get-url", "--push", "--all", name]
        )
        if push_urls and push_urls != fetch_urls:
            ops._run_git(
                snapshot_root, ["remote", "set-url", "--push", name, push_urls[0]]
            )
            for url in push_urls[1:]:
                ops._run_git(
                    snapshot_root, ["remote", "set-url", "--add", "--push", name, url]
                )


def _optional_git_lines(
    ops: SnapshotServices, /, cwd: Path, args: list[str]
) -> list[str]:
    completed = subprocess.run(
        [ops._git_executable(cwd), *args],
        cwd=cwd,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line]


def _git_executable(ops: SnapshotServices, /, cwd: Path) -> str:
    if not ops._is_windows_platform():
        executable = shutil.which("git")
        if executable is None:
            raise ops.WorkspaceSnapshotError(
                "git executable is required for workspace snapshots"
            )
        # Preserve the existing POSIX invocation/search contract.
        return "git"
    try:
        return ops.require_trusted_executable("git", cwd=cwd, windows=True)
    except ops.ExecutableResolutionError as exc:
        raise ops.WorkspaceSnapshotError(str(exc)) from exc


def _run_git(
    ops: SnapshotServices, /, cwd: Path, args: list[str], *, capture_bytes: bool = False
) -> str | bytes:
    completed = subprocess.run(
        [ops._git_executable(cwd), *args],
        cwd=cwd,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"exit {completed.returncode}"
        raise ops.WorkspaceSnapshotError(
            f"git {' '.join(args)} failed in {cwd}: {detail}"
        )
    if capture_bytes:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace")


def _run_git_apply(
    ops: SnapshotServices, /, cwd: Path, args: list[str], patch: bytes
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [ops._git_executable(cwd), "apply", *args],
        cwd=cwd,
        env=ops._isolated_git_env(),
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _isolated_git_env(ops: SnapshotServices, /) -> dict[str, str]:
    env = os.environ.copy()
    blocked = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for key in list(env):
        if key in blocked or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def snapshot_git_environment(ops: SnapshotServices, /) -> dict[str, str]:
    return ops._isolated_git_env()


def _git_control_is_trusted(ops: SnapshotServices, /, self: WorkspaceSnapshot) -> bool:
    git_dir = self.snapshot_root / ".git"
    if ops.is_link_or_reparse(git_dir) or not git_dir.is_dir():
        return False
    if not ops._regular_file_matches(git_dir / "config", self.git_config_bytes):
        return False
    worktree_config = git_dir / "config.worktree"
    if self.git_worktree_config_bytes is None:
        return not (worktree_config.exists() or worktree_config.is_symlink())
    return ops._regular_file_matches(worktree_config, self.git_worktree_config_bytes)


def _restore_git_control(ops: SnapshotServices, /, self: WorkspaceSnapshot) -> bool:
    if self.git_control_is_trusted():
        return False
    try:
        ops._restore_trusted_snapshot_git_config(self)
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to restore trusted snapshot Git config: {exc}"
        ) from exc
    return True
