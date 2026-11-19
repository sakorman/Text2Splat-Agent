import sqlite3

import pytest

from graphics_mcp.store import Store


@pytest.fixture
def connections(monkeypatch):
    # Keep native connections alive so the assertions cannot pass through GC.
    opened = []
    connect = sqlite3.connect

    def track(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", track)
    yield opened
    for db in opened:
        db.close()


def assert_closed(connections):
    assert connections
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_initialization_closes_connection(tmp_path, connections):
    Store(tmp_path)
    assert_closed(connections)


def test_idle_queue_polling_closes_connections_without_gc(tmp_path, connections):
    store = Store(tmp_path)
    for _ in range(100):
        assert store.claim() is None
    assert_closed(connections)


def test_durable_job_lifecycle_and_links_close_connections(tmp_path, connections):
    store = Store(tmp_path)
    payload = {"prompt": "一个青瓷花瓶"}
    job = store.submit("reference", "reference", payload)
    assert store.submit("reference", "reference", payload) == job
    assert store.by_request("reference") == job
    assert store.by_request("missing") is None
    assert store.claim() == (job["id"], "reference", payload)
    assert store.claim() is None
    assert store.update(job["id"], stage="image")["stage"] == "image"
    assert_closed(connections)

    restarted = Store(tmp_path)
    restarted.recover()
    interrupted = restarted.get(job["id"])
    assert interrupted["status"] == "failed"
    assert restarted.update(job["id"], status="succeeded") == interrupted
    assert restarted.claim() is None
    receipt = {"job_id": job["id"], "status": "rejected"}
    assert restarted.link("reconstruction", receipt) == receipt
    assert Store(tmp_path).link("reconstruction") == receipt
    assert restarted.link("missing") is None
    assert_closed(connections)


@pytest.mark.parametrize("operation", ["conflicting_request", "invalid_id", "missing_job"])
def test_rejected_operations_close_connections(tmp_path, connections, operation):
    store = Store(tmp_path)
    job = store.submit("reference", "reference", {"prompt": "一个花瓶"})
    with pytest.raises(ValueError):
        if operation == "conflicting_request":
            store.submit("reference", "reference", {"prompt": "一只小鸟"})
        elif operation == "invalid_id":
            store.get("invalid")
        else:
            store.update("f" * 32, status="failed")
    assert store.get(job["id"]) == job
    assert_closed(connections)


def test_body_failure_rolls_back_before_closing(tmp_path, connections):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError, match="interrupted"):
        with store.transaction() as db:
            db.execute("INSERT INTO links VALUES (?, ?)", ("uncommitted", "{}"))
            raise RuntimeError("interrupted")
    assert_closed(connections)
    assert Store(tmp_path).link("uncommitted") is None


def test_commit_failure_rolls_back_and_closes(tmp_path, connections):
    store = Store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with store.transaction() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) "
                       "DEFERRABLE INITIALLY DEFERRED)")
            db.execute("INSERT INTO child VALUES (1)")
            assert db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 1
    assert_closed(connections)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        db.execute("INSERT INTO parent VALUES (1)")
        db.execute("INSERT INTO child VALUES (1)")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 1
    assert_closed(connections)
