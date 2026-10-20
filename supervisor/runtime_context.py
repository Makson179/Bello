"""Read-only runtime ledger evidence outside private supervisor state."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from supervisor.filesystem_safety import is_link_or_reparse, remove_path_tree


class RuntimeContextStore:
    """Own exact copies of omitted ledger text for one stateless runtime wake.

    Runtime tools must not read the private ``.supervisor`` tree. The controller
    already owns these two ledger strings in the wake packet, so export only
    the needed strings, never discover or copy other private state files.
    """

    def __init__(self, *, progress: str | None = None, decisions: str | None = None) -> None:
        self.root: Path | None = None
        self.paths: dict[str, str] = {}
        self._files: dict[Path, str] = {}
        ledgers = {"progress": progress, "decisions": decisions}
        if all(value is None for value in ledgers.values()):
            return
        self.root = Path(tempfile.mkdtemp(prefix="bello-runtime-inputs-")).resolve()
        try:
            for name, text in ledgers.items():
                if text is None:
                    continue
                path = self.root / f"{name.upper()}.md"
                data = text.encode("utf-8")
                with path.open("xb") as handle:
                    handle.write(data)
                path.chmod(0o400)
                self._files[path] = hashlib.sha256(data).hexdigest()
                self.paths[f"{name}_path"] = str(path)
            self.root.chmod(0o500)
        except BaseException:
            self.cleanup()
            raise

    @property
    def readable_roots(self) -> list[Path]:
        # File authorities deliberately exclude the containing directory and
        # all live supervisor state, including logs, sessions and credentials.
        return list(self._files)

    def assert_unchanged(self) -> None:
        if self.root is None:
            return
        if is_link_or_reparse(self.root) or not self.root.is_dir():
            raise OSError("runtime evidence root was removed or replaced")
        for path, expected in self._files.items():
            if is_link_or_reparse(path) or not path.is_file():
                raise OSError("runtime evidence file was removed or replaced")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise OSError("runtime evidence was modified during review")

    def cleanup(self) -> None:
        if self.root is not None:
            remove_path_tree(self.root)
        self.paths.clear()
        self._files.clear()
