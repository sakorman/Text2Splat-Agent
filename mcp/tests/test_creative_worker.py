"""Real worker-thread/SQLite tests with offline image providers, never real artwork."""
import threading
import time

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from graphics_mcp.server import create_server
from graphics_mcp.store import Store
from text2splat.tools import decode
from test_pipeline import setup

PRIVATE_ERROR = "test-only-private-storage-details"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.01)
    assert predicate(), "worker did not reach the expected state"


@pytest.fixture
def worker_rig(setup, monkeypatch):
    _, store, providers, worker = setup
    errors, gates = [], []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_type.__name__))
    try:
        yield store, providers, worker, errors, gates
    finally:
        worker.stop.set()
        for gate in gates:
            gate.set()
        if getattr(worker, "thread", None):
            worker.thread.join(3)
            assert not worker.thread.is_alive()


@pytest.mark.parametrize("boundary", ["before_claim", "after_commit"])
def test_claim_fault_keeps_worker_alive_without_replaying_unknown_claim(worker_rig, monkeypatch, caplog, boundary):
    store, providers, worker, errors, gates = worker_rig
    ready = threading.Event()
    gates.append(ready)
    claim, injected = store.claim, False

    def faulty_claim():
        nonlocal injected
        assert ready.wait(3)
        if not injected:
            injected = True
            if boundary == "after_commit":
                assert claim() is not None  # Receipt lost after the real transaction committed.
            raise OSError(PRIVATE_ERROR)
        return claim()

    monkeypatch.setattr(store, "claim", faulty_claim)
    worker.start()
    first = worker.reference("一个青瓷花瓶", "first")
    second = worker.reference("一个木雕小鸟", "second")
    ready.set()
    wait_for(lambda: store.get(second["id"])["status"] == "succeeded" or not worker.thread.is_alive())

    assert worker.thread.is_alive() and not errors
    assert store.get(second["id"])["status"] == "succeeded"
    assert store.get(first["id"])["status"] == ("running" if boundary == "after_commit" else "succeeded")
    assert providers.calls == (1 if boundary == "after_commit" else 2)
    assert worker.reference("一个青瓷花瓶", "first")["id"] == first["id"]
    assert PRIVATE_ERROR not in caplog.text

    worker.stop.set()
    worker.thread.join(3)
    recovered = Store(store.root)
    recovered.recover()
    if boundary == "after_commit":
        assert recovered.get(first["id"])["status"] == "failed"
        assert "未" in recovered.get(first["id"])["message"] or "不会" in recovered.get(first["id"])["message"]
    assert recovered.claim() is None
    assert providers.calls == (1 if boundary == "after_commit" else 2)


def test_failed_completion_writes_do_not_kill_worker_or_reissue_image(worker_rig, monkeypatch, caplog):
    store, providers, worker, errors, gates = worker_rig
    ready, claim = threading.Event(), store.claim
    gates.append(ready)

    def after_setup():
        assert ready.wait(3)
        return claim()

    monkeypatch.setattr(store, "claim", after_setup)
    worker.start()
    # Both requests are created before the first image callback can set the fault.
    first = worker.reference("一个青瓷花瓶", "lost-local-completion")
    second = worker.reference("一个木雕小鸟", "next")
    update, failing = store.update, False

    def image_arrived():
        nonlocal failing
        failing = True

    def broken_completion(jid, **fields):
        if jid == first["id"] and failing:
            raise OSError(PRIVATE_ERROR)
        return update(jid, **fields)

    providers.on_image = image_arrived
    monkeypatch.setattr(store, "update", broken_completion)
    ready.set()
    wait_for(lambda: store.get(second["id"])["status"] == "succeeded" or not worker.thread.is_alive())

    assert worker.thread.is_alive() and not errors
    assert store.get(second["id"])["status"] == "succeeded" and providers.calls == 2
    unknown = store.get(first["id"])
    assert unknown["status"] == "running" and "asset_set_id" not in unknown
    assert worker.reference("一个青瓷花瓶", "lost-local-completion")["id"] == first["id"]
    assert PRIVATE_ERROR not in caplog.text
    failing = False
    worker.cancel(first["id"])
    assert store.get(first["id"])["status"] == "cancelled" and providers.calls == 2


def test_shutdown_interrupts_backoff_during_persistent_claim_fault(worker_rig, monkeypatch):
    store, providers, worker, errors, _ = worker_rig
    attempted = threading.Event()
    attempts = 0

    def unavailable():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            attempted.set()
        raise OSError(PRIVATE_ERROR)

    monkeypatch.setattr(store, "claim", unavailable)
    worker.start()
    assert attempted.wait(3), "worker stopped instead of polling after a claim error"
    worker.stop.set()
    worker.thread.join(1)
    assert not worker.thread.is_alive() and not errors
    assert attempts == 2 and providers.calls == 0


def test_failed_task_can_be_recorded_after_its_first_two_writes_fail(worker_rig, monkeypatch, caplog):
    store, providers, worker, errors, gates = worker_rig
    ready, claim, update = threading.Event(), store.claim, store.update
    gates.append(ready)

    def after_setup():
        assert ready.wait(3)
        return claim()

    monkeypatch.setattr(store, "claim", after_setup)
    worker.start()
    first = worker.reference("一个青瓷花瓶", "failed-task")
    second = worker.reference("一个木雕小鸟", "following-task")
    failures = 0

    def fail_twice(jid, **fields):
        nonlocal failures
        if jid == first["id"] and failures < 2:
            failures += 1
            raise OSError(PRIVATE_ERROR)
        return update(jid, **fields)

    monkeypatch.setattr(store, "update", fail_twice)
    ready.set()
    wait_for(lambda: store.get(second["id"])["status"] == "succeeded" or not worker.thread.is_alive())
    assert worker.thread.is_alive() and not errors and failures == 2
    assert store.get(first["id"])["status"] == "failed"
    assert store.get(second["id"])["status"] == "succeeded" and providers.calls == 1
    assert PRIVATE_ERROR not in caplog.text + str(store.get(first["id"]))


def test_startup_recovery_failure_is_not_hidden(worker_rig, monkeypatch):
    store, providers, worker, _, _ = worker_rig

    def unavailable():
        raise OSError(PRIVATE_ERROR)

    monkeypatch.setattr(store, "recover", unavailable)
    with pytest.raises(OSError):
        worker.start()
    assert worker.thread is None and worker.health()["worker_running"] is False
    assert providers.calls == 0


def test_health_reports_actual_worker_lifecycle(worker_rig):
    _, _, worker, errors, _ = worker_rig
    assert worker.health()["configured"] is True
    assert worker.health()["worker_running"] is False
    worker.start()
    assert worker.health()["worker_running"] is True
    worker.stop.set()
    assert worker.health()["worker_running"] is False
    worker.thread.join(1)
    assert worker.health()["worker_running"] is False and not errors


def test_real_mcp_session_reports_worker_state_without_calling_provider(setup):
    settings, _, providers, _ = setup
    server, worker = create_server("creative", settings, 0)
    worker.providers = providers

    async def check():
        async with create_connected_server_and_client_session(server) as session:
            async def health():
                result = await session.call_tool("creative_health", {})
                assert settings.zhipu_key not in str(result)
                return decode(result)

            assert (await health())["worker_running"] is False
            worker.start()
            assert (await health())["worker_running"] is True
            worker.stop.set()
            assert (await health())["worker_running"] is False

    try:
        anyio.run(check)
        assert providers.calls == 0
    finally:
        worker.stop.set()
        if worker.thread:
            worker.thread.join(3)
            assert not worker.thread.is_alive()
