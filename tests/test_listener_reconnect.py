import asyncio
import sys
import types

import pytest

from app.delivery import worker as worker_mod
from app.delivery.worker import listen_for_notifications


class _FakeWorker:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


class _FakeConn:
    def __init__(self):
        self.closed = False
        self._term = None

    def add_termination_listener(self, cb):
        self._term = cb

    async def add_listener(self, channel, cb):
        pass

    async def close(self):
        self.closed = True


def _fake_asyncpg(*, fail_first=True, drop_second=True):
    mod = types.ModuleType("asyncpg")

    class PostgresError(Exception):
        pass

    mod.PostgresError = PostgresError
    state = {"attempts": 0, "conns": []}

    async def connect(dsn):
        state["attempts"] += 1
        if fail_first and state["attempts"] == 1:
            raise OSError("connection refused")
        conn = _FakeConn()
        state["conns"].append(conn)
        if drop_second and state["attempts"] == 2:
            # fire termination AFTER the listener is wired (lazy lambda)
            asyncio.get_running_loop().call_later(0.02, lambda: conn._term(conn))
        return conn

    mod.connect = connect
    mod._state = state
    return mod


@pytest.mark.asyncio
async def test_reconnects_after_failure_and_drop(monkeypatch):
    monkeypatch.setattr(worker_mod, "LISTENER_BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(worker_mod, "LISTENER_BACKOFF_MAX", 0.05)
    fake = _fake_asyncpg()
    monkeypatch.setitem(sys.modules, "asyncpg", fake)

    stop = asyncio.Event()
    worker = _FakeWorker()
    task = asyncio.create_task(listen_for_notifications("postgresql://x", "ch", worker, stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert fake._state["attempts"] >= 3            # failed once, reconnected ≥ twice
    assert worker.wakes >= 2                        # woke on each successful (re)connect
    assert all(c.closed for c in fake._state["conns"])  # no leaked connections


@pytest.mark.asyncio
async def test_stops_promptly_without_hanging(monkeypatch):
    monkeypatch.setattr(worker_mod, "LISTENER_BACKOFF_INITIAL", 0.01)
    fake = _fake_asyncpg(fail_first=False, drop_second=False)
    monkeypatch.setitem(sys.modules, "asyncpg", fake)

    stop = asyncio.Event()
    worker = _FakeWorker()
    task = asyncio.create_task(listen_for_notifications("postgresql://x", "ch", worker, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done() and worker.wakes >= 1