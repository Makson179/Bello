"""Pi-specific host configuration shared by catalog discovery and execution."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path


def pi_agent_directory(*, env: Mapping[str, str] | None = None,
                       cwd: Path | None = None, home: Path | None = None) -> Path:
    """Resolve Pi's agent directory without creating files or reading credentials.

    Empty overrides fall through, matching the login and worker JS resolver.
    Relative overrides are relative to the launcher, not the worker installation.
    """
    values = os.environ if env is None else env
    home = Path.home() if home is None else home
    configured = values.get("BELLO_PI_AGENT_DIR") or values.get("PI_CODING_AGENT_DIR")
    path = Path(configured) if configured else home / ".pi" / "agent"
    if str(path) == "~":
        path = home
    elif str(path).startswith(("~/", "~" + os.sep)):
        path = home / str(path)[2:]
    if not path.is_absolute():
        path = (Path.cwd() if cwd is None else cwd) / path
    return Path(os.path.abspath(path))
