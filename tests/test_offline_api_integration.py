"""Post-roadmap #3: one document indexed by the offline pipeline is searchable through the public
/search endpoint and fetchable through /doc/{sha}, with identities, URLs and hashes agreeing end to
end - all on an ephemeral localhost server with no Ollama and no external network."""

import hashlib
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.extract.html import extract
from openagentsearch.extract.store import ExtractStore
from openagentsearch.pipeline.index import index_document
from openagentsearch.vector.store import VectorStore

TARGET = "ALPHA TARGET"
NAV_PHRASE = "NAVBOILERPLATE-MENU-LINKS"
SOURCE_URL = "https://docs.example.test/alpha"
RAW_HTML = (
    '<html lang="en"><head><title>Alpha Document</title></head><body>'
    f"<nav>{NAV_PHRASE}</nav>"
    "<p>Ordinary introductory text sits here first.</p>"
    "<p>ALPHA TARGET appears only here.</p>"
    "</body></html>"
)
CHUNK_SIZE = 44
OVERLAP = 0
FETCHED_AT = 1700000000.0
EXTRACTED_AT = 1700000001.0
SEARCH_KEYS = ["query", "k", "results"]
RESULT_KEYS = ["chunk_id", "doc_sha256", "doc_url", "score", "snippet"]
DOC_KEYS = ["doc_sha256", "url", "title", "lang", "text", "extracted_at", "provenance"]
PROVENANCE_KEYS = ["url", "fetched_at", "status", "sha256", "robots_allowed"]


class StubEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if TARGET in text else [0.0, 1.0]


class Fixture:
    """Temp root + VectorStore rows from the real pipeline + ExtractStore document + one live server."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VectorStore(self.root / "vectors.sqlite", dimension=2)
        self.embedder = StubEmbedder()
        self.report = index_document(
            RAW_HTML, SOURCE_URL, store=self.store, embedder=self.embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP
        )
        self.extracted = extract(RAW_HTML)
        raw_bytes = RAW_HTML.encode("utf-8")
        assert hashlib.sha256(raw_bytes).hexdigest() == self.report.doc_sha256
        (self.root / "raw").mkdir()
        (self.root / "raw" / f"{self.report.doc_sha256}.html").write_bytes(raw_bytes)
        self.provenance = {
            "url": SOURCE_URL, "fetched_at": FETCHED_AT, "status": 200,
            "sha256": self.report.doc_sha256, "robots_allowed": True,
        }
        (self.root / "raw" / "provenance.jsonl").write_text(
            json.dumps(self.provenance, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        ExtractStore(self.root).put(self.report.doc_sha256, SOURCE_URL, self.extracted, EXTRACTED_AT)
        known = self.report.doc_sha256
        self.server = create_server(
            "127.0.0.1", 0,
            routes={"/search": make_search_route(self.store, self.embedder, lambda sha: SOURCE_URL if sha == known else None)},
            prefix_routes={"/doc/": make_doc_route(self.root)},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def target_chunk_id(self) -> str:
        holders = [cid for cid in self.report.chunk_ids if TARGET in str(self.store.get(cid)["text"])]
        assert len(holders) == 1, holders
        return holders[0]

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=10)
        finally:
            self.store.close()
            self._tmp.cleanup()


def test_indexed_document_is_searchable_over_http():
    fx = Fixture()
    try:
        target = fx.target_chunk_id()
        status, body = fx.get(f"/search?q=ALPHA+TARGET+query&k={fx.report.chunk_count}")
        assert status == 200
        assert list(body.keys()) == SEARCH_KEYS
        assert body["query"] == "ALPHA TARGET query" and body["k"] == fx.report.chunk_count
        assert len(body["results"]) >= 1
        first = body["results"][0]
        assert first["chunk_id"] == target
        assert first["doc_sha256"] == fx.report.doc_sha256
        assert first["doc_url"] == SOURCE_URL
        assert first["score"] == 1.0
        assert TARGET in first["snippet"]
        for result in body["results"]:
            assert list(result.keys()) == RESULT_KEYS
            assert NAV_PHRASE not in result["snippet"]
    finally:
        fx.close()


def test_search_result_document_is_fetchable_over_doc_endpoint():
    fx = Fixture()
    try:
        status, search = fx.get("/search?q=ALPHA+TARGET+query&k=1")
        assert status == 200 and len(search["results"]) == 1
        first = search["results"][0]
        assert first["doc_sha256"] == fx.report.doc_sha256
        status, doc = fx.get(f"/doc/{first['doc_sha256']}")
        assert status == 200
        assert list(doc.keys()) == DOC_KEYS
        assert doc["doc_sha256"] == fx.report.doc_sha256
        assert doc["url"] == SOURCE_URL
        assert doc["title"] == fx.extracted["title"] == "Alpha Document"
        assert doc["lang"] == fx.extracted["lang"] == "en"
        assert doc["text"] == fx.extracted["text"]
        assert hashlib.sha256(doc["text"].encode("utf-8")).hexdigest() == fx.report.extracted_text_sha256
        assert doc["extracted_at"] == EXTRACTED_AT
        assert doc["provenance"] is not None
        assert list(doc["provenance"].keys()) == PROVENANCE_KEYS
        assert doc["provenance"] == fx.provenance
        assert doc["provenance"]["sha256"] == fx.report.doc_sha256 and doc["provenance"]["url"] == SOURCE_URL
        assert TARGET in doc["text"] and NAV_PHRASE not in doc["text"]
    finally:
        fx.close()


def test_health_search_and_doc_share_one_offline_server():
    fx = Fixture()
    try:
        assert fx.get("/healthz") == (200, {"status": "ok"})
        status, search = fx.get("/search?q=ALPHA+TARGET&k=1")
        assert status == 200 and len(search["results"]) == 1
        result = search["results"][0]
        status, doc = fx.get(f"/doc/{result['doc_sha256']}")
        assert status == 200
        assert result["doc_url"] == doc["url"]
        assert result["doc_sha256"] == doc["doc_sha256"]
        unknown = "0" * 63 + "1"
        assert fx.get(f"/doc/{unknown}") == (404, {"error": "not_found"})
        assert fx.get("/healthz") == (200, {"status": "ok"})
    finally:
        fx.close()
