import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.delivery.ssrf import resolve_and_validate, SSRFError
from app.delivery.http import attempt_delivery


@pytest.mark.parametrize("url, blocked", [
    ("http://127.0.0.1/x", True),
    ("http://localhost/x", True),
    ("http://169.254.169.254/latest", True),     # cloud metadata
    ("http://10.0.0.5/x", True),
    ("http://192.168.1.1/x", True),
    ("http://[::1]/x", True),
    ("http://[::ffff:127.0.0.1]/x", True),        # ipv4-mapped bypass attempt
    ("ftp://example.com/x", True),                # disallowed scheme
    ("http://93.184.216.34/x", False),            # public literal
])
@pytest.mark.asyncio
async def test_ssrf_validation(url, blocked):
    if blocked:
        with pytest.raises(SSRFError):
            await resolve_and_validate(url)
    else:
        target = await resolve_and_validate(url)
        assert target.ip == "93.184.216.34"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        routes = {"/ok": (200, b"ok"), "/gone": (410, b"gone"), "/fail": (500, b"boom")}
        if self.path == "/slow":
            time.sleep(2); code, msg = 200, b"late"
        else:
            code, msg = routes.get(self.path, (404, b"nope"))
        self.send_response(code)
        self.send_header("content-length", str(len(msg)))
        self.end_headers()
        try:
            self.wfile.write(msg)
        except BrokenPipeError:
            pass   # client abandoned a slow request; expected


@pytest.fixture(scope="module")
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.mark.asyncio
async def test_2xx_succeeds(client, local_server):
    r = await attempt_delivery(client, url=f"{local_server}/ok", payload="{}",
                               message_id="evt_1", allow_private=True)
    assert r.succeeded and r.response_status == 200 and r.latency_ms is not None


@pytest.mark.asyncio
async def test_5xx_is_retryable(client, local_server):
    r = await attempt_delivery(client, url=f"{local_server}/fail", payload="{}",
                               message_id="evt_1", allow_private=True)
    assert not r.succeeded and r.retryable and r.response_status == 500


@pytest.mark.asyncio
async def test_410_is_not_retryable(client, local_server):
    r = await attempt_delivery(client, url=f"{local_server}/gone", payload="{}",
                               message_id="evt_1", allow_private=True)
    assert not r.succeeded and not r.retryable and r.response_status == 410


@pytest.mark.asyncio
async def test_timeout_is_retryable(client, local_server):
    r = await attempt_delivery(
        client, url=f"{local_server}/slow", payload="{}", message_id="evt_1",
        allow_private=True,
        timeout=httpx.Timeout(connect=3.0, read=0.5, write=3.0, pool=3.0),
    )
    assert not r.succeeded and r.retryable and r.error == "timeout"


@pytest.mark.asyncio
async def test_guard_blocks_loopback_by_default(client, local_server):
    r = await attempt_delivery(client, url=f"{local_server}/ok", payload="{}",
                               message_id="evt_1")  # allow_private defaults False
    assert not r.succeeded and not r.retryable and r.error.startswith("blocked")