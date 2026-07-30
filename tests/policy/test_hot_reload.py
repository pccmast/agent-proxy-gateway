"""Tests for policy hot-reload wiring (Bug2 fix).

start_watching() exists but was never called from lifespan; this guards that
the watcher thread actually triggers reload() when a config YAML changes and
that stop_watching() cleanly terminates it.
"""

import time

import pytest

from gateway.policy.store import PolicyStore

_MINIMAL_POLICY = """
proxy: {}
trace: {}
guardrails: {}
budget: {}
rate_limit: {}
circuit_breaker: {}
eval: {}
"""


def _load(store: PolicyStore) -> None:
    store.reload()


def test_watcher_triggers_reload_on_change(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    policy_file = config_dir / "policy.yaml"
    policy_file.write_text(_MINIMAL_POLICY)

    store = PolicyStore(config_dir=str(config_dir))
    store.reload()

    calls = {"n": 0}
    original = store.reload

    def counting_reload() -> None:
        calls["n"] += 1
        original()

    store.reload = counting_reload  # type: ignore[method-assign]

    # Mutate the YAML and deterministically force the watcher to detect a
    # "change". We poison the cached mtime to 0.0 instead of relying on real
    # filesystem mtime resolution (back-to-back writes can be < the 0.01s
    # threshold on Windows and make the test flaky).
    policy_file.write_text(_MINIMAL_POLICY + "proxy:\n  host: 127.0.0.1\n")
    store._file_mtimes[str(policy_file)] = 0.0

    store.start_watching(interval=0.2)
    try:
        for _ in range(60):  # up to ~6s
            if calls["n"] >= 1:
                break
            time.sleep(0.1)
        assert calls["n"] >= 1
    finally:
        store.stop_watching()


def test_stop_watching_joins_thread(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "policy.yaml").write_text(_MINIMAL_POLICY)

    store = PolicyStore(config_dir=str(config_dir))
    store.reload()
    store.start_watching(interval=0.2)
    # Thread should be alive while watching.
    assert store._watch_thread is not None and store._watch_thread.is_alive()
    store.stop_watching()
    assert store._watch_thread is None
