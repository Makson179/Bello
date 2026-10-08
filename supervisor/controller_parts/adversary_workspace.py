"""Controller adversary workspace; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _create_adversary_snapshot(
    project_root: compat.Path,
    *,
    excluded_relative_paths: tuple[str, ...] = (),
) -> compat.Path:
    temp_root = compat.Path(compat.tempfile.mkdtemp(prefix="bello-adversary-")).resolve()
    snapshot_root = temp_root / "workspace"
    try:
        compat.copy_isolated_workspace_tree(
            project_root,
            snapshot_root,
            ignore=compat._adversary_snapshot_ignore_with_paths(
                project_root,
                excluded_relative_paths,
            ),
        )
    except Exception:
        try:
            compat.remove_isolated_workspace_tree(temp_root)
        except OSError:
            pass
        raise
    compat._init_snapshot_git(snapshot_root)
    return snapshot_root


def _init_snapshot_git(snapshot_root: compat.Path) -> None:
    """Give the snapshot a functional git repo so tests/tools that shell out to git work.

    Best-effort: an empty initial commit makes HEAD/status/diff usable while keeping every
    file untracked, so recursive deletes inside the snapshot stay policy-approvable.
    """
    git = compat._controller_executable("git", snapshot_root, environ=compat.snapshot_git_environment())
    if git is None:
        return
    identity = [
        "-c",
        "user.email=bello@localhost",
        "-c",
        "user.name=Bello Snapshot",
        "-c",
        "commit.gpgsign=false",
    ]
    git_env = compat.snapshot_git_environment()
    try:
        initialized = compat.subprocess.run(
            [git, "-c", "init.templateDir=", "init", "-q"],
            cwd=snapshot_root,
            env=git_env,
            stdout=compat.subprocess.DEVNULL,
            stderr=compat.subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if initialized.returncode != 0:
            return
        compat.subprocess.run(
            [git, "config", "--local", "core.hooksPath", compat.os.devnull],
            cwd=snapshot_root,
            env=git_env,
            stdout=compat.subprocess.DEVNULL,
            stderr=compat.subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        compat.subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                *identity,
                "commit",
                "-q",
                "--no-verify",
                "--allow-empty",
                "-m",
                "bello adversary snapshot baseline",
            ],
            cwd=snapshot_root,
            env=git_env,
            stdout=compat.subprocess.DEVNULL,
            stderr=compat.subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return


def _adversary_snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        ".git",
        ".supervisor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    if compat.is_windows_platform():
        ignored_keys = {name.casefold() for name in ignored}
        return {name for name in names if name.casefold() in ignored_keys}
    return {name for name in names if name in ignored}


def _adversary_snapshot_ignore_with_paths(
    project_root: compat.Path,
    excluded_relative_paths: tuple[str, ...],
):
    root = project_root.resolve()
    if compat.is_windows_platform():
        excluded = {compat.Path(path).as_posix().casefold() for path in excluded_relative_paths}
    else:
        excluded = {compat.Path(path).as_posix() for path in excluded_relative_paths}

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = compat._adversary_snapshot_ignore(directory, names)
        try:
            relative_directory = compat.Path(directory).resolve().relative_to(root)
        except (OSError, ValueError):
            return ignored
        for name in names:
            relative_path = (relative_directory / name).as_posix()
            key = relative_path.casefold() if compat.is_windows_platform() else relative_path
            if key in excluded:
                ignored.add(name)
        return ignored

    return ignore
