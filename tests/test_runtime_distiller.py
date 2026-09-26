from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from supervisor.runtime import distiller as module
from supervisor.runtime import distiller_worker as worker
from supervisor.runtime.distiller import LogDistiller, require_dependencies
from supervisor.runtime.distiller_worker import make_entry


@pytest.mark.parametrize("text", ["plain ASCII", "café \u2014 résumé", "Ошибка: 文件 😀\r\nnext line"])
def test_worker_emits_utf8_json_independent_of_stdout_encoding(monkeypatch, text):
    pipe = io.BytesIO()
    stdout = io.TextIOWrapper(pipe, encoding="cp1252", errors="strict", newline="\r\n")
    response = {"id": 1, "ok": True, "text": text}
    with monkeypatch.context() as patch:
        patch.setattr(worker.sys, "stdout", stdout)
        worker.emit(response)
    payload = pipe.getvalue()
    assert payload == (json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert json.loads(payload) == response


def test_dependency_preflight_checks_presence_without_importing(monkeypatch):
    before = {name: sys.modules.get(name) for name in ("torch", "transformers", "safetensors", "huggingface_hub")}
    checked = []

    def available(name):
        checked.append(name)
        return object()

    monkeypatch.setattr(module.importlib.util, "find_spec", available)
    require_dependencies()
    assert checked == ["torch", "transformers", "safetensors", "huggingface_hub"]
    assert {name: sys.modules.get(name) for name in checked} == before


def test_dependency_preflight_lists_missing_packages_and_install_extra(monkeypatch):
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object() if name == "torch" else None)
    with pytest.raises(RuntimeError, match="transformers, safetensors") as error:
        require_dependencies()
    assert "Bello[log-distiller]" in str(error.value)
    assert "this Python environment" in str(error.value)


FAKE_WORKER = r'''
import json, os, sys, time
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["command"] == "hang":
        time.sleep(60)
    if request["command"] == "slow":
        time.sleep(.15)
    if request["command"] == "crash":
        sys.exit(9)
    if request["command"] == "invalid":
        print("not-json", flush=True)
        continue
    text = request["log"] if request["command"] == "same" else request["log"][:3]
    if request["command"] == "larger":
        text = request["log"] + "!"
    print(json.dumps({"id": request["id"], "ok": True, "text": text}), flush=True)
'''


@pytest.fixture
def fake_worker(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec

    def install(source=FAKE_WORKER):
        processes = []

        async def spawn(*args, **kwargs):
            assert args[:3] == (sys.executable, "-m", "supervisor.runtime.distiller_worker")
            assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
            assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
            process = await real_spawn(sys.executable, "-u", "-c", source, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        return processes

    return install


async def wait_for_worker(distiller):
    async with asyncio.timeout(3):
        while distiller._process is None:
            await asyncio.sleep(.005)


def long_log(prefix):
    """Keep lifecycle/protocol tests above the small-output bypass boundary."""
    return prefix + "\n" + "repeated diagnostic detail\n" * 10


@pytest.mark.parametrize("text", [
    "", "x", "x" * 199, "x" * 200, "é" * 100, "😀" * 50,
    "Ошибка\r\n文件\t😀",
])
async def test_small_outputs_bypass_without_loading_or_queueing(tmp_path, monkeypatch, text):
    distiller = LogDistiller(tmp_path)
    unexpected_calls = []

    async def unexpected(*_args, **_kwargs):
        unexpected_calls.append(True)
        raise AssertionError("small outputs must not acquire a lock or start inference")

    monkeypatch.setattr(distiller._lock, "acquire", unexpected)
    monkeypatch.setattr(distiller, "_exchange", unexpected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    assert len(text.encode("utf-8")) <= 200
    assert await distiller.distill(text, "focus", "cmd") == text
    assert unexpected_calls == []  # Fail-open must not hide a forbidden call.
    assert distiller._sequence == 0
    assert distiller._request is None
    assert distiller._process is None and distiller._spawn is None
    assert not distiller._unavailable
    await distiller.close()


@pytest.mark.parametrize("text", ["x" * 201, "é" * 101, "😀" * 51])
async def test_outputs_above_200_utf8_bytes_still_infer(tmp_path, fake_worker, text):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    try:
        assert len(text.encode("utf-8")) > 200
        assert await distiller.distill(text, "focus", "cmd") == text[:3]
        assert len(processes) == 1 and distiller._sequence == 1
    finally:
        await distiller.close()


async def test_small_output_does_not_wait_for_or_stop_warm_worker(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    try:
        assert await distiller.distill(long_log("first log"), "focus", "cmd") == "fir"
        async with distiller._lock:
            text = "é" * 100
            assert await asyncio.wait_for(distiller.distill(text, "focus", "cmd"), .5) == text
        assert len(processes) == 1 and processes[0].returncode is None
        assert distiller._sequence == 1
        assert await distiller.distill(long_log("next log"), "focus", "cmd") == "nex"
        assert distiller._sequence == 2
    finally:
        await distiller.close()


@pytest.mark.parametrize("route", ["native", "tool_host"])
@pytest.mark.parametrize("text", ["x" * 199, "x" * 200, "x" * 201, "é" * 100, "é" * 101])
async def test_cutoff_is_shared_by_native_and_claude_pi_tool_routes(tmp_path, fake_worker, route, text):
    from types import SimpleNamespace

    from supervisor.runtime.codex_distiller import CodexDistillerBridge
    from supervisor.runtime.tools import ToolHost

    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    eligible = len(text.encode("utf-8")) > 200
    try:
        if route == "native":
            bridge = CodexDistillerBridge(distiller, tmp_path)
            selected = await bridge._select({"log": text, "focus": "Find failure", "command": "pytest -q"})
        else:
            packet = {"details": {"output": text, "exitCode": 0},
                      "content": [{"type": "text", "text": "original packet"}], "isError": False}
            host = SimpleNamespace(distill=distiller.distill)
            result = await ToolHost._distill_result(host, packet, "exec_command",
                                                    {"focus": "Find failure", "command": "pytest -q"})
            selected = result["details"]["output"]
            assert result["details"]["exitCode"] == 0
            if not eligible:
                assert result is packet
        assert selected == (text[:3] if eligible else text)
        assert len(processes) == int(eligible)
        assert distiller._sequence == int(eligible)
    finally:
        await distiller.close()


async def test_lazy_persistent_worker_and_close(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    assert processes == []
    assert await distiller.distill("", "focus", "cmd") == ""
    original = long_log("original")
    assert await distiller.distill(original, "", "cmd") == original
    assert processes == []
    assert await distiller.distill(long_log("first log"), "focus", "cmd") == "fir"
    assert await distiller.distill(long_log("second log"), "focus", "cmd") == "sec"
    assert len(processes) == 1
    await distiller.close()
    assert processes[0].returncode is not None
    after_close = long_log("after close")
    assert await distiller.distill(after_close, "focus", "cmd") == after_close
    await distiller.close()


async def test_real_worker_protocol_preserves_unicode_with_cp1252_stdio(tmp_path, fake_worker):
    processes = fake_worker(r'''
import sys
from supervisor.runtime import distiller_worker as worker
sys.stdin.reconfigure(encoding="cp1252", errors="strict")
sys.stdout.reconfigure(encoding="cp1252", errors="strict", newline="\r\n")
class Selector:
    def select(self, text, focus, command):
        assert focus == "проверка 文件"
        assert command == "Get-Content café.txt"
        return text.split("\nignored", 1)[0]
worker.load_selector = lambda directory: Selector()
raise SystemExit(worker.main(["--model-path", "."]))
''')
    distiller = LogDistiller(tmp_path)
    try:
        for selected in ("café \u2014 résumé", "Ошибка: 文件 😀", "next ASCII result"):
            original = selected + "\nignored repeated diagnostic text" * 10
            assert await distiller.distill(original, "проверка 文件", "Get-Content café.txt") == selected
        assert len(processes) == 1 and processes[0].returncode is None
    finally:
        await distiller.close()


async def test_parallel_calls_are_serialized_and_all_selected(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    try:
        results = await asyncio.gather(*(distiller.distill(long_log(text), "focus", "slow")
                                         for text in ("first log", "second log", "third log")))
        assert results == ["fir", "sec", "thi"]
        assert len(processes) == 1
    finally:
        await distiller.close()


async def test_waiting_call_deadline_does_not_kill_active_worker(tmp_path, fake_worker, monkeypatch, caplog):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    active = asyncio.create_task(distiller.distill(long_log("active original"), "focus", "slow"))
    try:
        await wait_for_worker(distiller)
        monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", .03)
        waiting = long_log("waiting original")
        assert await distiller.distill(waiting, "focus", "cmd") == waiting
        assert await active == "act"
        assert len(processes) == 1 and processes[0].returncode is None
        assert "TimeoutError" in caplog.text
    finally:
        await distiller.close()


@pytest.mark.parametrize("command", ["hang", "invalid", "crash"])
async def test_failed_request_returns_original_and_reaps_before_restart(tmp_path, fake_worker, monkeypatch, command):
    processes = fake_worker()
    monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", .4)
    distiller = LogDistiller(tmp_path)
    try:
        original = long_log("original log")
        assert await distiller.distill(original, "focus", command) == original
        assert len(processes) == 1 and processes[0].returncode is not None
        assert await distiller.distill(long_log("next request"), "focus", "cmd") == "nex"
        assert len(processes) == 2
    finally:
        await distiller.close()


async def test_hard_deadline_also_covers_initial_model_load(tmp_path, fake_worker, monkeypatch):
    processes = fake_worker("import time; time.sleep(60)")
    monkeypatch.setattr(module, "REQUEST_TIMEOUT_SECONDS", .4)
    distiller = LogDistiller(tmp_path)
    original = long_log("original log")
    assert await distiller.distill(original, "focus", "cmd") == original
    assert len(processes) == 1 and processes[0].returncode is not None
    await distiller.close()


async def test_missing_bundle_disables_repeated_model_start(tmp_path, fake_worker):
    processes = fake_worker('print(\'{"error":"model_bundle_unavailable"}\', flush=True)')
    distiller = LogDistiller(tmp_path)
    original, next_request = long_log("original log"), long_log("next request")
    assert await distiller.distill(original, "focus", "cmd") == original
    assert await distiller.distill(next_request, "focus", "cmd") == next_request
    assert len(processes) == 1 and processes[0].returncode is not None
    await distiller.close()


@pytest.mark.parametrize("command", ["same", "larger"])
async def test_non_reducing_output_preserves_exact_input(tmp_path, fake_worker, command):
    fake_worker()
    distiller = LogDistiller(tmp_path)
    try:
        original = long_log("α\noriginal\r\n")
        assert await distiller.distill(original, "focus", command) == original
    finally:
        await distiller.close()


async def test_close_interrupts_inflight_model_without_cancelling_caller(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    original = long_log("original log")
    active = asyncio.create_task(distiller.distill(original, "focus", "hang"))
    await wait_for_worker(distiller)
    await asyncio.wait_for(distiller.close(), 3)
    assert await active == original
    assert processes[0].returncode is not None


async def test_caller_cancellation_reaps_child_and_remains_cancellation(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    active = asyncio.create_task(distiller.distill(long_log("original log"), "focus", "hang"))
    await wait_for_worker(distiller)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(active, 3)
    assert processes[0].returncode is not None
    assert await distiller.distill(long_log("next request"), "focus", "cmd") == "nex"
    await distiller.close()


async def test_cancelled_waiter_preserves_other_call(tmp_path, fake_worker):
    processes = fake_worker()
    distiller = LogDistiller(tmp_path)
    active = asyncio.create_task(distiller.distill(long_log("active request"), "focus", "slow"))
    await wait_for_worker(distiller)
    waiting = asyncio.create_task(distiller.distill(long_log("waiting request"), "focus", "cmd"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await active == "act"
    assert processes[0].returncode is None
    await distiller.close()


async def test_host_import_does_not_import_model_dependencies():
    process = await asyncio.create_subprocess_exec(sys.executable, "-c",
        "import sys; from supervisor.runtime.distiller import LogDistiller, validate_bundle; "
        "assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules; "
        "assert 'supervisor.runtime.distiller_worker' not in sys.modules")
    assert await process.wait() == 0


class PairEncoding(dict):
    def sequence_ids(self, index):
        return self["sequences"][index]


class TinyTokenizer:
    """An offset tokenizer exercising overlap without optional dependencies."""
    def num_special_tokens_to_add(self, pair=True):
        return 3

    def __call__(self, text, pair=None, **kwargs):
        if pair is None:
            if text.startswith("Focus:"):
                return {"input_ids": [99]}
            return {"input_ids": list(range(len(text))),
                    "offset_mapping": [(i, i + 1) for i in range(len(text))]}
        capacity = kwargs["max_length"] - 4
        encoded = PairEncoding(input_ids=[], offset_mapping=[], sequences=[])
        start = 0
        while start < len(pair):
            stop = min(len(pair), start + capacity)
            log = list(range(start, stop))
            encoded["input_ids"].append([100, 99, 101] + log + [102])
            encoded["offset_mapping"].append([(0, 0)] * 3 + [(i, i + 1) for i in log] + [(0, 0)])
            encoded["sequences"].append([None, 0, None] + [1] * len(log) + [None])
            if stop == len(pair):
                break
            start = stop - kwargs["stride"]
        return encoded


def test_overlapping_windows_preserve_first_prediction_ownership():
    windows, spans = make_entry("abcdefghijklmnopq", "focus", "cmd", TinyTokenizer(), max_length=12, overlap=3)
    assert [index for window in windows for _, index in window["owners"]] == list(range(17))
    assert [len(window["owners"]) for window in windows] == [8, 5, 4]
    assert windows[1]["input_ids"][3:6] == [5, 6, 7]  # context retained but never re-owned
    assert windows[1]["owners"][0] == (6, 8)
    assert spans == [[i, i + 1] for i in range(17)]


def test_full_conditioning_is_never_silently_truncated():
    with pytest.raises(ValueError, match="conditioning_exceeds_window_capacity"):
        make_entry("abc", "focus", "cmd", TinyTokenizer(), max_length=7, overlap=3)
