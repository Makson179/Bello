"""Durable ownership and at-most-once dispatch for model tool requests.

A request recorded as running when the host dies is *uncertain*, not retryable.
The model can inspect the workspace and issue a new request; Bello never replays
the old command on the assumption that it did not run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from supervisor.filesystem_safety import is_link_or_reparse


class JournalError(RuntimeError):
    pass


class RuntimeJournal:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        # Do not follow a repository-provided state-directory redirect. Parent
        # OS aliases (for example /tmp on macOS) are not repository inputs.
        for parent in [self.directory, *self.directory.parents]:
            if parent.name in {".supervisor", "engines"} and is_link_or_reparse(parent):
                raise JournalError("runtime state directory cannot be a symbolic link or reparse point")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_link_or_reparse(self.directory) or not self.directory.is_dir():
            raise JournalError("runtime journal must be a private directory, not a symbolic link")
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        path = self.directory / "runtime.sqlite3"
        for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if is_link_or_reparse(candidate):
                raise JournalError("runtime journal cannot be a symbolic link or reparse point")
            if candidate.exists():
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise JournalError("runtime journal must be an ordinary, unshared file")
        self._db = sqlite3.connect(path, isolation_level=None)
        if os.name != "nt":
            os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, data TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS tools ("
            "thread_id TEXT NOT NULL, call_id TEXT NOT NULL, fingerprint TEXT NOT NULL,"
            "status TEXT NOT NULL, result TEXT, PRIMARY KEY(thread_id, call_id));"
        )

    def close(self) -> None:
        self._db.close()

    def save_thread(self, thread_id: str, data: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO threads VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (thread_id, json.dumps(data, ensure_ascii=False)),
        )

    def threads(self) -> dict[str, dict[str, Any]]:
        return {row[0]: json.loads(row[1]) for row in self._db.execute("SELECT id,data FROM threads")}

    def claim_tool(self, thread_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        fingerprint = hashlib.sha256(
            json.dumps([name, arguments], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            self._db.execute("INSERT INTO tools VALUES (?, ?, ?, 'running', NULL)", (thread_id, call_id, fingerprint))
            return None
        except sqlite3.IntegrityError:
            row = self._db.execute(
                "SELECT fingerprint,status,result FROM tools WHERE thread_id=? AND call_id=?", (thread_id, call_id)
            ).fetchone()
            if row is None or row[0] != fingerprint:
                raise JournalError("tool call id was reused for a different action")
            if row[1] != "completed":
                raise JournalError("tool outcome is uncertain or still running; the command will not be replayed")
            return json.loads(row[2])

    def complete_tool(self, thread_id: str, call_id: str, result: dict[str, Any]) -> None:
        cursor = self._db.execute(
            "UPDATE tools SET status='completed',result=? WHERE thread_id=? AND call_id=? AND status='running'",
            (json.dumps(result, ensure_ascii=False), thread_id, call_id),
        )
        if cursor.rowcount != 1:
            raise JournalError("cannot complete an unknown or already completed tool call")
