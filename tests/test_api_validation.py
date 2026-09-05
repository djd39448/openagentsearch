"""P4.4 input validation and error framing, exercised over the real HTTP server."""

import json
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.vector.store import VectorStore


class StubEmbedder:
    """Records every text it is asked to embed and returns a fixed 2-d vector."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


def _start(tmpdir: str):
    """Minimal seeded store + server; returns (server, thread, base_url, embedder, store)."""
    store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
    store.add("only", "doc-1", [1.0, 0.0], "the only chunk")
    embedder = StubEmbedder()
    server = create_server("127.0.0.1", 0, routes={"/search": make_search_route(store, embedder, lambda sha: None)})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}", embedder, store


def _stop(server, thread, store) -> None:
    server.shutdown()
    server.server_close()
    thread.join()
    store.close()


def _request(url: str, method: str = "GET"):
    """Return (status, headers, body bytes) for any status code."""
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.getcode(), response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_missing_empty_and_whitespace_q_return_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            for query in ("/search", "/search?q=", "/search?q=%20%20%20", "/search?q=&k=5"):
                status, _, body = _request(base + query)
                assert status == 400, query
                assert json.loads(body) == {"error": "missing_query"}, query
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)


def test_512_char_q_accepted_and_513_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            ok_q = "a" * 512
            status, _, body = _request(base + "/search?q=" + ok_q)
            assert status == 200
            assert json.loads(body)["query"] == ok_q
            assert embedder.calls == [ok_q]

            status, _, body = _request(base + "/search?q=" + "a" * 513)
            assert status == 400
            assert json.loads(body) == {"error": "query_too_long"}
            assert embedder.calls == [ok_q]  # the rejected request was not embedded
        finally:
            _stop(server, thread, store)


def test_k_bounds_1_and_50_are_accepted():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            for k in ("1", "50"):
                status, _, body = _request(base + "/search?q=alpha&k=" + k)
                assert status == 200, k
                assert json.loads(body)["k"] == int(k)
            assert embedder.calls == ["alpha", "alpha"]
        finally:
            _stop(server, thread, store)


def test_k_out_of_range_returns_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            for k in ("0", "51"):
                status, _, body = _request(base + "/search?q=alpha&k=" + k)
                assert status == 400, k
                assert json.loads(body) == {"error": "invalid_k"}, k
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)


def test_malformed_k_returns_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            for k in ("abc", "1.5", " 7 ", "+7", "-1", "", "１２"):  # last one is fullwidth "１２"
                status, _, body = _request(base + "/search?q=alpha&k=" + urllib.parse.quote(k))
                assert status == 400, repr(k)
                assert json.loads(body) == {"error": "invalid_k"}, repr(k)
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)


def test_error_responses_carry_accurate_content_length():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, base, embedder, store = _start(tmpdir)
        try:
            status, headers, body = _request(base + "/nope")
            assert status == 404
            assert body == b'{"error":"not_found"}'
            assert headers["Content-Length"] == str(len(body))

            status, headers, body = _request(base + "/healthz", method="POST")
            assert status == 405
            assert body == b'{"error":"method_not_allowed"}'
            assert headers["Content-Length"] == str(len(body))
            assert headers["Allow"] == "GET, HEAD"

            status, headers, body = _request(base + "/nope", method="HEAD")
            assert status == 404
            assert body == b""
            assert headers["Content-Length"] == str(len(b'{"error":"not_found"}'))
        finally:
            _stop(server, thread, store)
