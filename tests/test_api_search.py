"""Tests for GET /search over the real HTTP server, with an injected stub embedder (never Ollama)."""

import json
import math
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
    """Records every text it is asked to embed and returns a fixed vector."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vector)


def _seed(tmpdir: str) -> VectorStore:
    store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
    store.add("along", "doc-1", [1.0, 0.0], "alpha " * 50 + "tail")  # 304 chars -> snippet is cut
    store.add("across", "doc-2", [0.0, 1.0], "orthogonal text")
    store.add("between", "doc-1", [1.0, 1.0], "diagonal text")
    return store


def _serve(store: VectorStore, embedder: StubEmbedder, resolver):
    server = create_server("127.0.0.1", 0, routes={"/search": make_search_route(store, embedder, resolver)})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop(server, thread, store: VectorStore) -> None:
    server.shutdown()
    server.server_close()
    thread.join()
    store.close()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_search_returns_ranked_results_over_http():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seed(tmpdir)
        embedder = StubEmbedder([1.0, 0.0])
        server, thread, base = _serve(store, embedder, lambda sha: f"https://example.test/{sha}")
        try:
            status, body = _get(base + "/search?q=" + urllib.parse.quote(" alpha ") + "&k=2")

            assert status == 200
            assert set(body) == {"query", "k", "results"}
            assert body["query"] == " alpha "  # the original string, not stripped
            assert body["k"] == 2
            assert embedder.calls == [" alpha "]  # embedded exactly once, verbatim

            results = body["results"]
            assert [r["chunk_id"] for r in results] == ["along", "between"]  # cosine 1.0 then 1/sqrt(2)
            assert results[0]["score"] == 1.0
            assert math.isclose(results[1]["score"], 1 / math.sqrt(2))
            for r in results:
                assert set(r) == {"chunk_id", "doc_sha256", "doc_url", "score", "snippet"}
                assert r["doc_sha256"] == "doc-1"
                assert r["doc_url"] == "https://example.test/doc-1"
            assert results[0]["snippet"] == ("alpha " * 50 + "tail")[:200]
            assert len(results[0]["snippet"]) == 200
        finally:
            _stop(server, thread, store)


def test_k_defaults_to_ten_and_unknown_doc_url_is_null():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seed(tmpdir)
        embedder = StubEmbedder([1.0, 0.0])
        server, thread, base = _serve(store, embedder, lambda sha: None if sha == "doc-2" else f"u:{sha}")
        try:
            status, body = _get(base + "/search?q=anything")

            assert status == 200
            assert body["k"] == 10
            assert [r["chunk_id"] for r in body["results"]] == ["along", "between", "across"]  # all 3 records
            by_id = {r["chunk_id"]: r for r in body["results"]}
            assert by_id["across"]["doc_url"] is None  # JSON null for an unknown document
            assert by_id["along"]["doc_url"] == "u:doc-1"
        finally:
            _stop(server, thread, store)


def test_missing_q_returns_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seed(tmpdir)
        embedder = StubEmbedder([1.0, 0.0])
        server, thread, base = _serve(store, embedder, lambda sha: None)
        try:
            status, body = _get(base + "/search")
            assert status == 400
            assert body == {"error": "missing_query"}
            status, body = _get(base + "/search?k=3")
            assert status == 400
            assert body == {"error": "missing_query"}
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)


def test_blank_q_returns_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seed(tmpdir)
        embedder = StubEmbedder([1.0, 0.0])
        server, thread, base = _serve(store, embedder, lambda sha: None)
        try:
            for query in ("/search?q=", "/search?q=%20%20", "/search?q=&k=2"):
                status, body = _get(base + query)
                assert status == 400
                assert body == {"error": "missing_query"}
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)


def test_invalid_k_returns_400_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seed(tmpdir)
        embedder = StubEmbedder([1.0, 0.0])
        server, thread, base = _serve(store, embedder, lambda sha: None)
        try:
            for bad_k in ("abc", "0", "-3", "1.5", ""):
                status, body = _get(base + "/search?q=alpha&k=" + urllib.parse.quote(bad_k))
                assert status == 400, bad_k
                assert body == {"error": "invalid_k"}, bad_k
            assert embedder.calls == []
        finally:
            _stop(server, thread, store)
