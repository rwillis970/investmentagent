"""HTTP transport for broker adapters that speak to a real API over HTTP
(§1.2, §11 Day 10: the Alpaca paper adapter is the first consumer).

One interface, two implementations -- the same shape as `agent.
secrets_provider.SecretsProvider`: a real implementation and a test double
that never touches the outside world, injected at construction rather than
selected by a runtime flag. `AlpacaPaperAdapter` never imports `urllib`
itself; it only ever calls `Transport.request`.

`UrllibTransport` uses Python's stdlib `urllib.request` -- no dependency
added (pyproject.toml stays empty). `ScriptedTransport` is the test double:
every response (or error) is enqueued in advance and returned in order, and
every call made is recorded, so a test can assert on exactly what an
adapter sent (method, path, params, body, timeout) without a socket ever
opening.

RETRY POLICY LIVES IN THE CALLER, NOT HERE. `Transport.request` makes
exactly one attempt and either returns or raises -- it has no opinion on
whether a failure is safe to retry, because that depends entirely on
whether the call being made is a read or a write, and only the caller
(`AlpacaPaperAdapter`) knows which one this is. See its module docstring
for why reads retry (bounded, via config) and writes never do.

TIMEOUT VS. OTHER TRANSPORT FAILURES -- the distinction that matters for a
write. `TransportTimeout` means the request may have reached the server
before the response was lost -- genuinely ambiguous. Every other
`TransportError` (connection refused, DNS failure, TLS failure) fails
before any request bytes could plausibly have reached the server, so it is
NOT ambiguous in the same way. Both are still exceptions a write's caller
must not treat as "definitely failed" without a second thought -- see
`agent.broker.alpaca.AmbiguousOrderState` for how `AlpacaPaperAdapter`
actually handles this (conservatively: ANY transport exception during a
write is treated as ambiguous, not just a timeout -- see that module's
docstring for why).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod


class TransportError(Exception):
    """A transport-level failure: connection refused, DNS failure, TLS
    failure, or any other networking problem that is not a timeout."""


class TransportTimeout(TransportError):
    """The request exceeded its timeout with no response. See module
    docstring: for a write, this is the dangerous, ambiguous case."""


class Transport(ABC):
    @abstractmethod
    def request(self, method: str, path: str, *, headers: dict[str, str],
               params: dict | None = None, json_body: dict | None = None,
               timeout: float) -> tuple[int, dict]:
        """Make exactly one HTTP request. Returns (status_code, parsed_json_
        body) for ANY response received, including non-2xx -- a 422 or 403
        is a normal, interpretable application response the caller needs
        the body to interpret, not a reason to raise. Raises
        `TransportTimeout` on a timeout, `TransportError` on any other
        transport-level failure (never returns a partial or guessed
        result)."""


class UrllibTransport(Transport):
    """The real transport. Stdlib `urllib.request` only."""

    def request(self, method: str, path: str, *, headers: dict[str, str],
               params: dict | None = None, json_body: dict | None = None,
               timeout: float) -> tuple[int, dict]:
        url = path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            # A non-2xx status IS the response, not a transport failure --
            # returned like any other, so the caller can interpret it (e.g.
            # a 422 duplicate-client_order_id from Alpaca).
            raw = exc.read()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            return exc.code, body
        except TimeoutError:
            raise TransportTimeout(f"{method} {path} timed out after {timeout}s") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TransportTimeout(f"{method} {path} timed out after {timeout}s") from None
            raise TransportError(f"{method} {path} failed: {exc.reason}") from None


class ScriptedTransport(Transport):
    """Test double. Never opens a socket. Enqueue responses (`enqueue`) or
    exceptions (`enqueue_error`) in advance; each `request()` call consumes
    the next one, in order. Every call is recorded in `.calls` for
    assertions -- this is how a test proves, e.g., that a paper-bound
    adapter's requests only ever target the paper base URL, or that a
    submit's body includes `client_order_id`."""

    def __init__(self):
        self._queue: list[tuple[str, object]] = []
        self.calls: list[dict] = []

    def enqueue(self, status: int, body: dict) -> None:
        self._queue.append(("response", (status, body)))

    def enqueue_error(self, exc: Exception) -> None:
        self._queue.append(("error", exc))

    def request(self, method: str, path: str, *, headers: dict[str, str],
               params: dict | None = None, json_body: dict | None = None,
               timeout: float) -> tuple[int, dict]:
        self.calls.append(dict(method=method, path=path, headers=dict(headers),
                               params=params, json_body=json_body, timeout=timeout))
        if not self._queue:
            raise AssertionError(
                "ScriptedTransport: no more responses queued for "
                f"{method} {path} -- enqueue one before this call"
            )
        kind, value = self._queue.pop(0)
        if kind == "error":
            raise value
        return value
