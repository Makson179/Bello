#!/usr/bin/env python3
"""Credential-free synthetic provider + persistent rollout parser reproduction.

Uses the actual supplied native helper, a localhost fake provider and a constant
selector. It neither downloads weights nor makes a paid/authenticated model call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify_native_codex_selection as native
from scripts import verify_native_codex_download as published_windows
from scripts.verify_windows_modernbert_live import digest, selected_history_evidence


async def verify(binary: Path, output: Path) -> dict:
    original_session = native.NativeSession
    thread = {}

    class PersistentFixtureSession(original_session):
        async def request(self, method, params):
            if method == "thread/start":
                params = {**params, "ephemeral": False, "model": "gpt-5.6-luna"}
            reply = await super().request(method, params)
            if method == "thread/start":
                thread.update(reply["thread"])
            return reply

        async def complete(self, thread_id, *, timeout=60):
            await super().complete(thread_id, timeout=timeout)
            reply = await self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            if reply["thread"]["id"] != thread_id:
                raise ValueError("Persistent fixture returned unrelated thread")
            thread.update(reply["thread"])

    # Only change persistence/model metadata. Execution, selector delivery,
    # provider boundary, environment isolation and sandbox remain the harness's.
    native.NativeSession = PersistentFixtureSession
    try:
        result = await native.run_case(binary, native.Case("persistent_direct"), output / "case")
    finally:
        native.NativeSession = original_session
    hashes, counts = selected_history_evidence(output / "case/empty-home", thread["id"], thread.get("path"))
    matched = digest(native.SELECTED) in hashes
    report = {"schema": "bello.native-persistent-history-smoke.v1", "paid_model_calls": 0,
              "live_coder_task": False, "provider_passed": result["passed"],
              "exact_selected_history_match": matched, "native_history_counts": counts,
              "passed": result["passed"] and matched}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--codex", type=Path)
    source.add_argument("--runtime-root", type=Path, help="Fresh Windows runtime root: download the published helper")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if args.runtime_root is not None:
        installed = published_windows.verify(args.runtime_root.absolute())
        (args.output_dir / "published-install.json").write_text(json.dumps(installed, indent=2) + "\n", encoding="utf-8")
        if installed.get("passed") is not True:
            raise ValueError("Published Windows helper is required")
        command, _ = published_windows.installer.ensure_native_selection()
        binary = Path(command[0])
    else:
        binary = args.codex.absolute()
    report = asyncio.run(verify(binary, args.output_dir.absolute()))
    print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
