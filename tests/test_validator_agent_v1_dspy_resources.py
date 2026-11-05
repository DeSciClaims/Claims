"""DSPy request threads release SQLite handles without losing cached results."""

from __future__ import annotations

import builtins
import gc
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from validator.agent_v1.dspy_resources import close_dspy_lm
from validator.agent_v1 import eligibility_dspy


def _closer(events, name, *, fail=False):
    def close():
        events.append((name, threading.get_ident()))
        if fail:
            raise RuntimeError("cleanup failed")
    return SimpleNamespace(close=close)


def test_cleanup_closes_model_client_and_injected_cache_in_calling_thread():
    events = []
    lm = _closer(events, "model")
    lm.client = _closer(events, "client")
    disk = _closer(events, "disk")
    memory = {"saved-response": "keep"}
    module = SimpleNamespace(cache=SimpleNamespace(disk_cache=disk, memory_cache=memory))
    with ThreadPoolExecutor(max_workers=1) as pool:
        thread = pool.submit(lambda: (close_dspy_lm(lm, dspy_module=module), threading.get_ident())).result()[1]
    assert {name for name, _ in events} == {"model", "client", "disk"}
    assert all(identity == thread for _, identity in events)
    assert memory == {"saved-response": "keep"}


def test_cleanup_uses_already_loaded_dspy_without_importing_it(monkeypatch):
    events = []
    module = SimpleNamespace(cache=SimpleNamespace(disk_cache=_closer(events, "disk")))
    monkeypatch.setitem(sys.modules, "dspy", module)
    original_import = builtins.__import__
    def no_dspy_import(name, *args, **kwargs):
        if name == "dspy" or name.startswith("dspy."):
            raise AssertionError("cleanup must not import and initialize DSPy")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_dspy_import)
    close_dspy_lm(None)
    assert [name for name, _ in events] == ["disk"]
    monkeypatch.delitem(sys.modules, "dspy")
    close_dspy_lm(None)


def test_cleanup_failures_do_not_mask_model_error_or_skip_other_resources():
    events = []
    lm = _closer(events, "model", fail=True)
    lm.client = _closer(events, "client", fail=True)
    module = SimpleNamespace(cache=SimpleNamespace(disk_cache=_closer(events, "disk", fail=True)))
    with pytest.raises(ValueError, match="original model failure"):
        try:
            raise ValueError("original model failure")
        finally:
            close_dspy_lm(lm, dspy_module=module)
    assert {name for name, _ in events} == {"model", "client", "disk"}


def _temporary_dspy_cache(tmp_path, monkeypatch):
    # Import the real implementation after selecting local-only model metadata;
    # these tests never construct an inference client or make model requests.
    # LiteLLM imports python-dotenv. Keep all SDK initialization in a private
    # environment so it cannot load repository .env values into other tests.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("DSPY_CACHEDIR", str(tmp_path / "sdk-cache"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("diskcache")
    from dspy.clients.cache import Cache
    cache = Cache(enable_disk_cache=True, enable_memory_cache=True,
                  disk_cache_dir=str(tmp_path), memory_max_entries=512)
    cache.disk_cache.close()
    return cache


def test_short_lived_request_threads_keep_descriptor_count_bounded_and_cache_reusable(tmp_path, monkeypatch):
    fd_directory = next((p for p in (Path("/proc/self/fd"), Path("/dev/fd")) if p.is_dir()), None)
    if fd_directory is None:
        pytest.skip("Unix descriptor inventory is unavailable")
    cache = _temporary_dspy_cache(tmp_path, monkeypatch)
    module = SimpleNamespace(cache=cache)
    gc.collect()
    before = len(os.listdir(fd_directory))
    was_enabled = gc.isenabled()
    gc.disable()  # Cleanup must be deterministic, independent of cyclic GC.
    growth = []
    try:
        for _ in range(4):
            barrier = threading.Barrier(2)
            def request(worker):
                try:
                    barrier.wait(timeout=5)
                    for key in range(64):
                        cache.put({"worker": worker, "key": key}, {"value": key})
                finally:
                    close_dspy_lm(None, dspy_module=module)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(request, range(2)))
            growth.append(len(os.listdir(fd_directory)) - before)
        # The unfixed implementation grows by hundreds of handles here.
        assert max(growth) <= 4, growth
        assert cache.memory_cache, "closing connections must retain the memory cache"
        cache.reset_memory_cache()
        assert cache.get({"worker": 1, "key": 63}) == {"value": 63}
        close_dspy_lm(None, dspy_module=module)
        assert len(os.listdir(fd_directory)) - before <= 4
    finally:
        cache.disk_cache.close()
        if was_enabled:
            gc.enable()
        gc.collect()


def test_one_thread_cleanup_does_not_close_another_threads_active_cache_transaction(tmp_path, monkeypatch):
    cache = _temporary_dspy_cache(tmp_path, monkeypatch)
    module = SimpleNamespace(cache=cache)
    ready, transaction_open, cleaned = (threading.Event() for _ in range(3))
    def first():
        try:
            cache.put({"first": 1}, {"value": "first"})
            ready.set()
            assert transaction_open.wait(5)
        finally:
            close_dspy_lm(None, dspy_module=module)
            cleaned.set()
    def second():
        try:
            assert ready.wait(5)
            with cache.disk_cache.transact():
                cache.put({"second": 2}, {"value": "second"})
                transaction_open.set()
                assert cleaned.wait(5)
                assert cache.get({"second": 2}) == {"value": "second"}
        finally:
            close_dspy_lm(None, dspy_module=module)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(first), pool.submit(second)]
        for future in futures:
            future.result(timeout=10)
    cache.reset_memory_cache()
    try:
        assert cache.get({"first": 1}) == {"value": "first"}
        assert cache.get({"second": 2}) == {"value": "second"}
    finally:
        cache.disk_cache.close()


class _EligibilityOutput(BaseModel):
    value: str


@pytest.mark.parametrize("outcome", ["success", "invalid_json", "model_error", "usage_error"])
def test_eligibility_runtime_finally_releases_current_thread_cache(outcome, monkeypatch):
    events = []
    lm = _closer(events, "model")
    lm.history = []
    module = SimpleNamespace(LM=lambda **_: lm, context=lambda **_: nullcontext(),
        cache=SimpleNamespace(disk_cache=_closer(events, "disk", fail=outcome == "model_error")))
    def program(**_):
        if outcome == "model_error":
            raise ValueError("original model failure")
        return SimpleNamespace(eligibility_json="not JSON") if outcome == "invalid_json" else {"value": "ok"}
    monkeypatch.setenv("CLAIMS_RESOURCE_TEST_KEY", "offline-fixture")
    monkeypatch.setattr(eligibility_dspy, "_constrained_output_model", lambda model, _: model)
    monkeypatch.setattr(eligibility_dspy.DSPyEligibilityRuntime, "_program", staticmethod(lambda *_: program))
    if outcome == "usage_error":
        def usage(_):
            raise RuntimeError("usage fixture failed")
        monkeypatch.setattr(eligibility_dspy, "usage_from_dspy_lm", usage)
    runtime = eligibility_dspy.DSPyEligibilityRuntime(provider="openrouter",
        api_base="https://openrouter.ai/api/v1", api_key_env="CLAIMS_RESOURCE_TEST_KEY", dspy_module=module)
    def run():
        return runtime.run(task={}, output_model=_EligibilityOutput, model="offline/model",
            stage_key="eligibility", stage_label="Eligibility", paper_id="paper", workspace_id="workspace")
    if outcome == "success":
        assert run() == _EligibilityOutput(value="ok")
    elif outcome == "model_error":
        with pytest.raises(ValueError, match="original model failure"):
            run()
    elif outcome == "invalid_json":
        with pytest.raises(ValueError, match="invalid JSON"):
            run()
    else:
        with pytest.raises(RuntimeError, match="usage fixture failed"):
            run()
    assert ("disk", threading.get_ident()) in events

