#!/usr/bin/env python3
"""Real published ModernBERT -> native Codex -> offline provider integration.

No paid model, training, threshold changes or live coding task. All log contents
are synthetic; the exact published selector recipe and production worker run on CPU.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify_native_codex_selection as native
from supervisor.runtime.codex_distiller import validate_native_selection
from supervisor.runtime.distiller import LogDistiller, REQUEST_TIMEOUT_SECONDS
from supervisor.runtime.distiller_bundle import validate_bundle
from supervisor.runtime.distiller_download import MODEL_REPOSITORY, MODEL_REVISION, ensure_default_bundle
from supervisor.runtime.distiller_worker import make_entry
from supervisor.runtime.native_codex_install import _private_directory, ensure_native_selection


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def token_count(tokenizer, text: str) -> int:
    return len(tokenizer(text, add_special_tokens=False, truncation=False)["input_ids"])


def synthetic_logs(tokenizer) -> dict[str, str]:
    noise = "PASS cached check: 1 2 3 4 5 6 7 8 9\n"
    failure = "FAILED tests/test_math.py::test_addition\nAssertionError: expected 3, received 4\n"
    short = noise * 24 + failure
    repeats = 9500 // token_count(tokenizer, noise) + 1
    long = noise * repeats + failure
    while token_count(tokenizer, long) < 9500:
        repeats += 1
        long = noise * repeats + failure
    # Exceed ModernBERT's window without hitting native's 10k approximate-token
    # output budget. Size is chosen mechanically, never by the model's decisions.
    if len(long.encode("utf-8")) >= 35000:
        raise ValueError("Synthetic long log exceeds the native output fixture budget")
    return {"short": short, "long": long}


class ObservedDistiller(LogDistiller):
    """Observe the real worker protocol without replacing inference or output."""
    def __init__(self, bundle: Path, tokenizer):
        super().__init__(bundle)
        self.tokenizer = tokenizer
        self.exchanges: list[str] = []
        self.measurements: list[dict] = []

    async def _exchange(self, request):
        result = await super()._exchange(request)
        self.exchanges.append(result)  # Only a valid worker id/ok/text reaches here.
        return result

    async def distill(self, text, focus, command):
        cold = self._process is None
        count = len(self.exchanges)
        started = time.perf_counter()
        selected = await super().distill(text, focus, command)
        elapsed = time.perf_counter() - started
        completed = len(self.exchanges) == count + 1
        same_worker_output = completed and self.exchanges[-1] == selected
        windows, _ = make_entry(text, focus, command, self.tokenizer)
        self.measurements.append({
            "cold": cold, "latency_seconds": elapsed,
            "latency_scope": "worker startup + model load + inference" if cold else "warm inference",
            "worker_pid": self._process.pid if self._process else None,
            "worker_response_ok": completed, "returned_worker_output": same_worker_output,
            "original_bytes": len(text.encode("utf-8")), "selected_bytes": len(selected.encode("utf-8")),
            "original_tokens": token_count(self.tokenizer, text),
            "selected_tokens": token_count(self.tokenizer, selected),
            "tokenizer": "published ModernBERT tokenizer; not coder billing tokens",
            "windows": len(windows), "input_sha256": digest(text), "selected_sha256": digest(selected),
            "strictly_reduced": 0 < len(selected.encode("utf-8")) < len(text.encode("utf-8")),
        })
        return selected


async def exercise(binary: Path, bundle: Path, output: Path, tokenizer) -> list[dict]:
    logs = synthetic_logs(tokenizer)
    results = []
    for size, log in logs.items():
        selector = ObservedDistiller(bundle, tokenizer)
        try:
            for cold in (True, False):
                name = f"{size}_{'cold' if cold else 'warm'}"
                before = len(selector.measurements)
                result = await native.run_case(binary, native.Case(name), output / name,
                    selector=selector, raw_output=log, turn_timeout=420)
                new = selector.measurements[before:]
                measurement = new[0] if len(new) == 1 else None
                real_selection = bool(measurement and measurement["cold"] is cold
                    and measurement["worker_response_ok"] and measurement["returned_worker_output"]
                    and measurement["strictly_reduced"] and result["bridge_outcomes"].get("changed") == 1)
                if not cold:
                    real_selection = real_selection and selector.measurements[0]["worker_pid"] == (
                        measurement["worker_pid"] if measurement else None)
                result.update(measurement=measurement, real_model_selection=real_selection,
                              passed=result["passed"] and real_selection)
                if size == "long":
                    result["passed"] = result["passed"] and bool(measurement and measurement["windows"] > 1)
                (output / name / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                results.append(result)
                print(json.dumps(result), flush=True)
            if size == "short":
                for protected in ("task", "help"):
                    before = len(selector.measurements)
                    name = f"{protected}_protected"
                    result = await native.run_case(binary, native.Case(name, protected=protected), output / name,
                        selector=selector, raw_output=log, turn_timeout=420)
                    result["model_bypassed"] = len(selector.measurements) == before
                    result["passed"] = result["passed"] and result["model_bypassed"]
                    (output / name / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                    results.append(result)
        finally:
            await selector.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This smoke requires native Windows")
    runtime, output = args.runtime_root.absolute(), args.output_dir.absolute()
    if os.path.lexists(runtime):
        raise ValueError("Use a fresh smoke runtime root")
    output.mkdir(parents=True, exist_ok=False)
    clean = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "USERNAME"}}
    home = runtime / "empty-home"
    clean.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home),
                 APPDATA=str(home / "appdata"), BELLO_RUNTIME_DIR=str(runtime / "native"),
                 HF_HOME=str(runtime / "huggingface"), HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                 HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="")
    os.environ.clear()
    os.environ.update(clean)
    _private_directory(home, parents=True)
    download_start = time.perf_counter()
    command, manifest_path = ensure_native_selection()
    capability = asyncio.run(validate_native_selection(command, manifest_path))
    native_download = time.perf_counter() - download_start
    download_start = time.perf_counter()
    bundle = ensure_default_bundle()
    model_download = time.perf_counter() - download_start
    recipe = validate_bundle(bundle)["recipe"]
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(bundle), local_files_only=True, use_fast=True, trust_remote_code=False)
    results = asyncio.run(exercise(Path(command[0]), bundle, output, tokenizer))
    report = {"schema": "bello.windows-real-modernbert-smoke.v1", "passed": all(r["passed"] for r in results),
              "paid_model_calls": 0, "live_coder_task": False, "model_repository": MODEL_REPOSITORY,
              "model_revision": MODEL_REVISION, "recipe": recipe, "selector_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
              "native_binary_sha256": capability["binary_sha256"], "platform": platform.platform(),
              "logical_cpus": os.cpu_count(), "torch": importlib.metadata.version("torch"),
              "transformers": importlib.metadata.version("transformers"), "model_download_seconds": model_download,
              "native_download_seconds": native_download, "cases": results,
              "not_covered": ["live coder task quality", "recall", "billing savings", "GPU inference"]}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cases": len(results)}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
