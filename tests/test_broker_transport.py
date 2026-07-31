"""HTTP transport for the Alpaca adapter (§1.2, §11 Day 10).

Two implementations behind one interface, so `AlpacaPaperAdapter` never
touches `urllib` (or a network socket) directly, and tests never make a real
network call -- `ScriptedTransport` is injected everywhere instead.

`UrllibTransport` is the real one: Python's stdlib `urllib.request`, no
dependency (pyproject.toml stays empty). These tests exercise
`UrllibTransport` against a real local HTTP server on loopback -- that is
NOT the network, it's a same-process, no-DNS, no-outbound-traffic loopback
socket, the same category of thing a unit test starting an in-memory
database would be. No test here, or anywhere in this codebase, ever makes a
call to an external host.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.broker.transport import (ScriptedTransport, Transport,
                                    TransportError, TransportTimeout,
                                    UrllibTransport)

# ------------------------------------------------------- request_raw (T4 prereq)
# Added for agent.edgar.EdgarClient.filing_document (T4 prerequisite unit,
# 2026-07-31): fetching a filing's HTML body is not a JSON API call, so
# `request()` (which always json.loads the body) cannot serve it.
# `request_raw` is the parallel primitive: (status, raw_bytes, truncated),
# with an optional `max_bytes` enforced during the read itself, not sliced
# off afterward -- see UrllibTransport's own implementation for why.


# ------------------------------------------------------------- ScriptedTransport

def test_scripted_transport_returns_enqueued_responses_in_order():
    t = ScriptedTransport()
    t.enqueue(200, {"a": 1})
    t.enqueue(201, {"b": 2})
    assert t.request("GET", "https://x/one", headers={}, timeout=1.0) == (200, {"a": 1})
    assert t.request("POST", "https://x/two", headers={}, timeout=1.0) == (201, {"b": 2})


def test_scripted_transport_raises_enqueued_errors():
    t = ScriptedTransport()
    t.enqueue_error(TransportTimeout("boom"))
    with pytest.raises(TransportTimeout, match="boom"):
        t.request("GET", "https://x/one", headers={}, timeout=1.0)


def test_scripted_transport_records_every_call():
    t = ScriptedTransport()
    t.enqueue(200, {})
    t.request("POST", "https://x/orders", headers={"H": "v"},
             params={"status": "open"}, json_body={"qty": "1"}, timeout=5.0)
    assert len(t.calls) == 1
    call = t.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "https://x/orders"
    assert call["headers"] == {"H": "v"}
    assert call["params"] == {"status": "open"}
    assert call["json_body"] == {"qty": "1"}
    assert call["timeout"] == 5.0


def test_scripted_transport_raises_assertion_error_when_exhausted():
    t = ScriptedTransport()
    with pytest.raises(AssertionError, match="no more responses queued"):
        t.request("GET", "https://x", headers={}, timeout=1.0)


def test_both_implementations_satisfy_the_same_interface():
    assert issubclass(UrllibTransport, Transport)
    assert issubclass(ScriptedTransport, Transport)


def test_scripted_transport_returns_enqueued_raw_bytes():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"<html>hello</html>")
    status, body, truncated = t.request_raw("GET", "https://x/doc.htm", headers={}, timeout=1.0)
    assert (status, body, truncated) == (200, b"<html>hello</html>", False)


def test_scripted_transport_raw_can_report_truncation():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"0123456789", truncated=True)
    status, body, truncated = t.request_raw("GET", "https://x/doc.htm", headers={}, timeout=1.0)
    assert truncated is True
    assert body == b"0123456789"


def test_scripted_transport_raw_raises_enqueued_errors():
    t = ScriptedTransport()
    t.enqueue_error(TransportTimeout("boom"))
    with pytest.raises(TransportTimeout, match="boom"):
        t.request_raw("GET", "https://x/doc.htm", headers={}, timeout=1.0)


def test_scripted_transport_raw_records_the_call_including_max_bytes():
    t = ScriptedTransport()
    t.enqueue_raw(200, b"abc")
    t.request_raw("GET", "https://x/doc.htm", headers={"User-Agent": "UA"},
                 timeout=2.0, max_bytes=100)
    assert len(t.calls) == 1
    call = t.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "https://x/doc.htm"
    assert call["headers"] == {"User-Agent": "UA"}
    assert call["timeout"] == 2.0
    assert call["max_bytes"] == 100


def test_scripted_transport_raw_raises_assertion_error_when_exhausted():
    t = ScriptedTransport()
    with pytest.raises(AssertionError, match="no more responses queued"):
        t.request_raw("GET", "https://x", headers={}, timeout=1.0)


def test_scripted_transport_raw_and_json_share_one_ordered_queue():
    """request() and request_raw() interleave against the SAME underlying
    queue, in enqueue order -- faithful to a real caller (e.g. EdgarClient)
    that makes both kinds of call against one shared rate limiter/session."""
    t = ScriptedTransport()
    t.enqueue(200, {"a": 1})
    t.enqueue_raw(200, b"raw-body")
    assert t.request("GET", "https://x/json", headers={}, timeout=1.0) == (200, {"a": 1})
    assert t.request_raw("GET", "https://x/raw", headers={}, timeout=1.0) == (200, b"raw-body", False)


def test_scripted_transport_raw_rejects_a_json_enqueue_used_as_raw():
    """Wiring a test wrong (enqueueing a JSON response but calling
    request_raw against it) fails loudly, not with a confusing type error
    deep inside the caller."""
    t = ScriptedTransport()
    t.enqueue(200, {"a": 1})
    with pytest.raises(AssertionError, match="request_raw"):
        t.request_raw("GET", "https://x", headers={}, timeout=1.0)


# ------------------------------------------------- UrllibTransport.request_raw
# Loopback HTTP server, not a network call -- see module docstring.

class _RawHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/big":
            body = b"0123456789" * 1000   # 10,000 bytes
        elif self.path == "/small":
            body = b"<html>tiny document</html>"
        elif self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        else:
            body = b""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def raw_server():
    httpd = HTTPServer(("127.0.0.1", 0), _RawHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=2)


def test_urllib_transport_raw_returns_full_body_under_the_cap(raw_server):
    transport = UrllibTransport()
    status, body, truncated = transport.request_raw(
        "GET", f"{raw_server}/small", headers={}, timeout=2.0, max_bytes=1_000_000)
    assert status == 200
    assert body == b"<html>tiny document</html>"
    assert truncated is False


def test_urllib_transport_raw_truncates_at_max_bytes_and_reports_it(raw_server):
    """The real point of this test: max_bytes is enforced DURING the read of
    a real socket, not sliced off a fully-buffered response afterward -- a
    10,000-byte body capped at 100 bytes must come back as EXACTLY 100
    bytes, with truncated=True, not silently as the full body."""
    transport = UrllibTransport()
    status, body, truncated = transport.request_raw(
        "GET", f"{raw_server}/big", headers={}, timeout=2.0, max_bytes=100)
    assert status == 200
    assert len(body) == 100
    assert body == (b"0123456789" * 1000)[:100]
    assert truncated is True


def test_urllib_transport_raw_with_no_cap_returns_everything(raw_server):
    transport = UrllibTransport()
    status, body, truncated = transport.request_raw(
        "GET", f"{raw_server}/big", headers={}, timeout=2.0, max_bytes=None)
    assert len(body) == 10_000
    assert truncated is False


def test_urllib_transport_raw_non_2xx_is_returned_not_raised(raw_server):
    transport = UrllibTransport()
    status, body, truncated = transport.request_raw(
        "GET", f"{raw_server}/missing", headers={}, timeout=2.0, max_bytes=1000)
    assert status == 404
    assert body == b"not found"


def test_urllib_transport_raw_timeout_raises_transport_timeout(server):
    transport = UrllibTransport()
    with pytest.raises(TransportTimeout):
        transport.request_raw("GET", f"{server}/slow", headers={}, timeout=0.05, max_bytes=1000)


def test_urllib_transport_raw_connection_failure_raises_transport_error():
    transport = UrllibTransport()
    with pytest.raises(TransportError):
        transport.request_raw("GET", "http://127.0.0.1:1", headers={}, timeout=1.0, max_bytes=1000)


# ------------------------------------------------------------- UrllibTransport
# Loopback HTTP server, not a network call -- see module docstring.

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep test output quiet

    def do_GET(self):
        if self.path == "/slow":
            import time
            time.sleep(0.5)
        body = json.dumps({"path": self.path, "method": "GET"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/reject":
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"code": 40010001, "message": "already exists"}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"echo": json.loads(raw)}).encode())


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=2)


def test_urllib_transport_get_returns_status_and_parsed_json(server):
    transport = UrllibTransport()
    status, body = transport.request("GET", f"{server}/account", headers={}, timeout=2.0)
    assert status == 200
    assert body == {"path": "/account", "method": "GET"}


def test_urllib_transport_post_sends_json_body(server):
    transport = UrllibTransport()
    status, body = transport.request("POST", f"{server}/orders", headers={},
                                     json_body={"symbol": "SPY"}, timeout=2.0)
    assert status == 200
    assert body == {"echo": {"symbol": "SPY"}}


def test_urllib_transport_non_2xx_is_returned_not_raised(server):
    """A 422 (e.g. Alpaca's duplicate client_order_id response) is a normal,
    interpretable application response -- AlpacaPaperAdapter needs the body
    to decide what happened, so this is returned as (422, body), not raised
    as an exception."""
    transport = UrllibTransport()
    status, body = transport.request("POST", f"{server}/reject", headers={},
                                     json_body={}, timeout=2.0)
    assert status == 422
    assert body["message"] == "already exists"


def test_urllib_transport_encodes_query_params(server):
    transport = UrllibTransport()
    status, body = transport.request("GET", f"{server}/orders", headers={},
                                     params={"status": "open"}, timeout=2.0)
    assert status == 200
    assert body["path"] == "/orders?status=open"


def test_urllib_transport_timeout_raises_transport_timeout(server):
    transport = UrllibTransport()
    with pytest.raises(TransportTimeout):
        transport.request("GET", f"{server}/slow", headers={}, timeout=0.05)


def test_urllib_transport_connection_failure_raises_transport_error():
    """Nothing is listening on this port -- a connection-refused case,
    distinct from a timeout: the request never reached any server."""
    transport = UrllibTransport()
    with pytest.raises(TransportError):
        transport.request("GET", "http://127.0.0.1:1", headers={}, timeout=1.0)
