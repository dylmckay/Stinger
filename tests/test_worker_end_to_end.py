import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import func, select

from app.crypto import get_secret_box
from app.models import Application, EventType, Endpoint, Event, Delivery, DeliveryAttempt
from app.delivery import signing
from app.delivery.worker import Worker

SECRET = signing.generate_secret()


class _Receiver(BaseHTTPRequestHandler):
    stats = {"received": 0, "verified": 0}
    lock = threading.Lock()

    def log_message(self, *a): pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        headers = {k.lower(): v for k, v in self.headers.items()}
        ok = signing.verify(body, headers, secret=SECRET)
        with _Receiver.lock:
            _Receiver.stats["received"] += 1
            _Receiver.stats["verified"] += int(ok)
        code = 500 if self.path == "/fail" else 200
        self.send_response(code)
        self.send_header("content-length", "1")
        self.end_headers()
        try:
            self.wfile.write(b"x")
        except BrokenPipeError:
            pass


@pytest.fixture
def receiver():
    _Receiver.stats = {"received": 0, "verified": 0}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Receiver.stats
    srv.shutdown()


@pytest.mark.asyncio
async def test_worker_delivers_signs_and_records(session_factory, receiver):
    base, stats = receiver
    n_ok, n_fail = 25, 25
    box = get_secret_box()

    async with session_factory() as s:
        app = Application(name="t"); s.add(app); await s.flush()
        et = EventType(application_id=app.id, name="invoice.paid"); s.add(et); await s.flush()
        ok = Endpoint(application_id=app.id, url=f"{base}/ok", secret=box.seal(SECRET))
        fail = Endpoint(application_id=app.id, url=f"{base}/fail", secret=box.seal(SECRET))
        s.add_all([ok, fail]); await s.flush()
        for i in range(n_ok + n_fail):
            ev = Event(application_id=app.id, event_type_id=et.id, payload=f'{{"seq":{i}}}')
            s.add(ev); await s.flush()
            s.add(Delivery(event_id=ev.id, endpoint_id=(ok if i < n_ok else fail).id, status="pending"))
        await s.commit()

    async with httpx.AsyncClient() as client:
        worker = Worker(session_factory, client, max_concurrency=20, poll_interval=0.2, allow_private=True)
        run_task = asyncio.create_task(worker.run())
        # Wait until all deliveries have been attempted (recorded in DeliveryAttempt).
        # This is more reliable than checking due==0, which only means deliveries
        # have been claimed, not that they've been dispatched and processed.
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with session_factory() as s:
                attempted = await s.scalar(
                    select(func.count()).select_from(DeliveryAttempt))
            if attempted == n_ok + n_fail:
                break
        worker.stop()
        await run_task

    async with session_factory() as s:
        succeeded = await s.scalar(select(func.count()).select_from(Delivery)
                                   .where(Delivery.status == "succeeded"))
        retrying = await s.scalar(select(func.count()).select_from(Delivery)
                                  .where(Delivery.status == "retrying"))
        attempts = await s.scalar(select(func.count()).select_from(DeliveryAttempt))

    assert stats["received"] == n_ok + n_fail
    assert stats["verified"] == stats["received"]   # every signature checked out
    assert succeeded == n_ok
    assert retrying == n_fail
    assert attempts == n_ok + n_fail