"""Reviewer backlog item 1: live fetch -> RawStore/provenance -> extraction -> atomic index, behind
the allowlist, robots.txt, the page budget and the rate limiter. One test drives a real local HTTP
server end to end; the rest use an injected fetcher so every refusal path is exercised with no
socket at all."""

import hashlib
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from openagentsearch.fetch.allowlist import AllowlistEntry
from openagentsearch.pipeline.index import index_document
from openagentsearch.pipeline.ingest import OUTCOMES, FetchResponse, IngestReport, LiveIngester, urllib_fetch
from openagentsearch.vector.store import VectorStore

PAGE_A = "<html><head><title>A</title></head><body><p>Alpha page about robots and crawling politely.</p></body></html>"
PAGE_A_TWIN = "<html><head><title>A twin</title></head><body><p>Alpha page about robots and crawling politely.</p></body></html>"
PAGE_B = "<html><head><title>B</title></head><body><p>Beta page about harbours, tides and ships at anchor.</p></body></html>"
ROBOTS = "User-agent: *\nDisallow: /private/\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Embedder:
    def __init__(self, fail_on_text: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on_text = fail_on_text

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail_on_text and self.fail_on_text in text:
            raise RuntimeError("embedder outage")
        return [1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0]


class FakeFetcher:
    """Maps url -> FetchResponse or Exception; records every call. Never opens a socket."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
        self.calls.append(url)
        answer = self.responses.get(url, FetchResponse(404, "text/plain", b"missing", False))
        if isinstance(answer, Exception):
            raise answer
        return answer


def _html(body: str, status: int = 200) -> FetchResponse:
    return FetchResponse(status, "text/html; charset=utf-8", body.encode("utf-8"), False)


def _ingester(root: Path, store: VectorStore, embedder, fetcher, *, hosts=(("example.test", 5),), sleep=None, clock=None):
    kwargs = dict(
        root=root, allowlist=[AllowlistEntry(h, n) for h, n in hosts], store=store, embedder=embedder,
        chunk_size=48, overlap=8, min_interval_s=0.0, fetcher=fetcher, sleep=sleep or (lambda s: None),
    )
    if clock is not None:
        kwargs["clock"] = clock
    return LiveIngester(**kwargs)


def _raw_files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "raw").glob("*.html")) if (root / "raw").exists() else []


def _provenance(root: Path) -> list[dict]:
    path = root / "raw" / "provenance.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------------------- real server


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        type(self).seen.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/a.html")
            self.end_headers()
            return
        entry = self.routes.get(self.path)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"nope")
            return
        content_type, body = entry
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_real_local_server_end_to_end():
    _Handler.routes = {
        "/robots.txt": ("text/plain", ROBOTS.encode()),
        "/a.html": ("text/html; charset=utf-8", PAGE_A.encode()),
        "/twin.html": ("text/html", PAGE_A_TWIN.encode()),
        "/b.html": ("text/html", PAGE_B.encode()),
        "/private/secret.html": ("text/html", PAGE_B.encode()),
        "/data.json": ("application/json", b'{"not": "html"}'),
    }
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = VectorStore(root / "vectors.sqlite3", 2)
            try:
                embedder = Embedder()
                ing = LiveIngester(
                    root=root, allowlist=[AllowlistEntry("127.0.0.1", 10)], store=store, embedder=embedder,
                    chunk_size=48, overlap=8, min_interval_s=0.0, timeout_s=5.0,
                )
                assert ing.fetcher is urllib_fetch

                a = ing.ingest(f"{base}/a.html")
                assert a.outcome == "indexed" and a.status == 200
                assert a.doc_sha256 == _sha(PAGE_A) and a.chunks_indexed >= 1
                assert store.count() == a.chunks_indexed
                assert _raw_files(root) == [f"{a.doc_sha256}.html"]
                assert (root / "raw" / f"{a.doc_sha256}.html").read_bytes() == PAGE_A.encode()
                prov = _provenance(root)
                assert len(prov) == 1 and prov[0]["url"] == f"{base}/a.html" and prov[0]["sha256"] == a.doc_sha256
                assert prov[0]["status"] == 200 and prov[0]["robots_allowed"] is True
                extracted = json.loads((root / "extracted" / f"{a.doc_sha256}.json").read_text(encoding="utf-8"))
                assert extracted["url"] == f"{base}/a.html" and extracted["title"] == "A" and "Alpha page" in extracted["text"]
                assert _Handler.seen == ["/robots.txt", "/a.html"]

                twin = ing.ingest(f"{base}/twin.html")  # same visible text, different bytes
                assert twin.outcome == "deduplicated" and twin.doc_sha256 == _sha(PAGE_A_TWIN)
                assert store.count() == a.chunks_indexed
                assert _raw_files(root) == sorted([f"{a.doc_sha256}.html", f"{twin.doc_sha256}.html"])  # raw kept
                assert not (root / "extracted" / f"{twin.doc_sha256}.json").exists()

                again = ing.ingest(f"{base}/a.html")  # unchanged page: its own text is already stored
                assert again.outcome == "deduplicated"
                assert store.count() == a.chunks_indexed and len(_provenance(root)) == 3

                b = ing.ingest(f"{base}/b.html")
                assert b.outcome == "indexed" and store.count() == a.chunks_indexed + b.chunks_indexed

                private = ing.ingest(f"{base}/private/secret.html")
                assert private.outcome == "refused_robots"
                assert "/private/secret.html" not in _Handler.seen  # never requested

                missing = ing.ingest(f"{base}/missing.html")
                assert missing.outcome == "http_error" and missing.status == 404

                redirect = ing.ingest(f"{base}/redirect")
                assert redirect.outcome == "http_error" and redirect.status == 302
                assert _Handler.seen.count("/a.html") == 2  # the redirect target was NOT followed

                data = ing.ingest(f"{base}/data.json")
                assert data.outcome == "refused_content_type"

                assert _Handler.seen.count("/robots.txt") == 1  # fetched once per host
                assert len(_raw_files(root)) == 3  # a, twin, b; refusals and errors stored nothing
                assert all(r.status == 200 for r in (a, b))
            finally:
                store.close()
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------------------- refusals


def test_host_outside_allowlist_is_never_contacted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({})
            ing = _ingester(root, store, Embedder(), fetcher)
            for url in ("http://other.test/page", "http://sub.example.test/page", "http://EXAMPLE.test.evil/x"):
                report = ing.ingest(url)
                assert report.outcome == "refused_allowlist", url
            assert ing.ingest("ftp://example.test/page").outcome == "refused_scheme"
            assert ing.ingest("http:///nohost").outcome == "refused_allowlist"
            assert fetcher.calls == []
            assert store.count() == 0 and _raw_files(root) == []
            with pytest.raises(ValueError):
                ing.ingest("")
        finally:
            store.close()


def test_robots_unavailable_fails_closed_and_404_allows():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({
                "http://example.test/robots.txt": FetchResponse(503, "text/plain", b"", False),
                "http://example.test/a.html": _html(PAGE_A),
            })
            ing = _ingester(root, store, Embedder(), fetcher)
            report = ing.ingest("http://example.test/a.html")
            assert report.outcome == "refused_robots_unavailable"
            assert ing.ingest("http://example.test/a.html").outcome == "refused_robots_unavailable"  # cached verdict
            assert fetcher.calls == ["http://example.test/robots.txt"]  # page never requested, robots asked once
            assert store.count() == 0

            fetcher2 = FakeFetcher({
                "http://example.test/robots.txt": OSError("connection refused"),
                "http://example.test/a.html": _html(PAGE_A),
            })
            ing2 = _ingester(root, store, Embedder(), fetcher2)
            assert ing2.ingest("http://example.test/a.html").outcome == "refused_robots_unavailable"
            assert fetcher2.calls == ["http://example.test/robots.txt"]

            fetcher3 = FakeFetcher({"http://example.test/a.html": _html(PAGE_A)})  # robots.txt -> 404
            ing3 = _ingester(root, store, Embedder(), fetcher3)
            assert ing3.ingest("http://example.test/a.html").outcome == "indexed"
            assert fetcher3.calls == ["http://example.test/robots.txt", "http://example.test/a.html"]
        finally:
            store.close()


def test_page_budget_stops_the_host_and_writes_the_marker():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({
                "http://example.test/robots.txt": FetchResponse(200, "text/plain", b"User-agent: *\nAllow: /\n", False),
                "http://example.test/a.html": _html(PAGE_A),
                "http://example.test/b.html": _html(PAGE_B),
            })
            ing = _ingester(root, store, Embedder(), fetcher, hosts=(("example.test", 1),))
            assert ing.ingest("http://example.test/a.html").outcome == "indexed"
            second = ing.ingest("http://example.test/b.html")
            assert second.outcome == "refused_budget"
            assert (root / "STOP-example.test").exists()
            assert json.loads((root / "STOP-example.test").read_text())["max_pages"] == 1
            assert "http://example.test/b.html" not in fetcher.calls
        finally:
            store.close()


def test_rate_limiter_spaces_requests_to_one_host():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            sleeps: list[float] = []
            now = [100.0]  # a clock that only advances when the limiter sleeps

            def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            fetcher = FakeFetcher({
                "http://example.test/a.html": _html(PAGE_A),
                "http://example.test/b.html": _html(PAGE_B),
            })
            ing = LiveIngester(
                root=root, allowlist=[AllowlistEntry("example.test", 5)], store=store, embedder=Embedder(),
                chunk_size=48, overlap=8, min_interval_s=2.0, fetcher=fetcher, sleep=fake_sleep,
            )
            ing.limiter.clock = lambda: now[0]
            ing.ingest("http://example.test/a.html")
            ing.ingest("http://example.test/b.html")
            # robots.txt, a.html, b.html -> each request after the first waits the full interval
            assert fetcher.calls == [
                "http://example.test/robots.txt", "http://example.test/a.html", "http://example.test/b.html",
            ]
            assert sleeps == [pytest.approx(2.0), pytest.approx(2.0)]
        finally:
            store.close()


def test_body_refusals_store_nothing():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({
                "http://example.test/big.html": FetchResponse(200, "text/html", b"<p>x</p>", True),
                "http://example.test/bin": FetchResponse(200, "application/octet-stream", b"\x00\x01", False),
                "http://example.test/latin1.html": FetchResponse(200, "text/html", "<p>caf\xe9</p>".encode("latin-1"), False),
                "http://example.test/down.html": TimeoutError("timed out"),
                "http://example.test/gone.html": FetchResponse(410, "text/html", b"<p>gone</p>", False),
            })
            ing = _ingester(root, store, Embedder(), fetcher)
            expected = {
                "http://example.test/big.html": "refused_too_large",
                "http://example.test/bin": "refused_content_type",
                "http://example.test/latin1.html": "refused_not_utf8",
                "http://example.test/down.html": "fetch_error",
                "http://example.test/gone.html": "http_error",
            }
            for url, outcome in expected.items():
                report = ing.ingest(url)
                assert report.outcome == outcome, url
                assert report.outcome in OUTCOMES
            assert store.count() == 0
            assert _raw_files(root) == [] and _provenance(root) == []
            assert not (root / "extracted").exists()
        finally:
            store.close()


# --------------------------------------------------------------------------------------- failure atomicity


def test_index_failure_keeps_provenance_removes_extracted_and_is_retryable():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({"http://example.test/a.html": _html(PAGE_A)})
            failing = Embedder(fail_on_text="Alpha")
            ing = _ingester(root, store, failing, fetcher)
            with pytest.raises(RuntimeError, match="embedder outage"):
                ing.ingest("http://example.test/a.html")
            sha = _sha(PAGE_A)
            assert store.count() == 0
            assert _raw_files(root) == [f"{sha}.html"]  # evidence of the fetch is kept
            assert len(_provenance(root)) == 1
            assert not (root / "extracted" / f"{sha}.json").exists()  # so the retry is not deduplicated away

            ing.embedder = Embedder()
            retry = ing.ingest("http://example.test/a.html")
            assert retry.outcome == "indexed" and retry.doc_sha256 == sha
            assert store.count() == retry.chunks_indexed
            assert (root / "extracted" / f"{sha}.json").exists()
            assert len(_provenance(root)) == 2
        finally:
            store.close()


def test_already_indexed_document_gets_its_extracted_record_without_reembedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            seed = Embedder()
            pre = index_document(PAGE_A, "http://example.test/a.html", store=store, embedder=seed, chunk_size=48, overlap=8)
            fetcher = FakeFetcher({"http://example.test/a.html": _html(PAGE_A)})
            embedder = Embedder()
            ing = _ingester(root, store, embedder, fetcher)
            report = ing.ingest("http://example.test/a.html")
            assert report.outcome == "already_indexed" and report.doc_sha256 == pre.doc_sha256
            assert embedder.calls == []
            assert store.count() == pre.chunks_indexed
            assert (root / "extracted" / f"{pre.doc_sha256}.json").exists()
        finally:
            store.close()


def test_ingest_many_reports_errors_and_continues():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            fetcher = FakeFetcher({
                "http://example.test/a.html": _html(PAGE_A),
                "http://example.test/b.html": _html(PAGE_B),
            })
            ing = _ingester(root, store, Embedder(fail_on_text="Alpha"), fetcher)
            reports = ing.ingest_many([
                "http://example.test/b.html", "http://example.test/a.html", "http://other.test/x", 42,
            ])
            assert [r.outcome for r in reports] == ["indexed", "error", "refused_allowlist", "error"]
            assert "embedder outage" in reports[1].detail and all(isinstance(r, IngestReport) for r in reports)
            assert store.count() == reports[0].chunks_indexed
        finally:
            store.close()


def test_constructor_rejects_unusable_configuration():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            common = dict(root=root, store=store, embedder=Embedder(), chunk_size=48, overlap=8)
            with pytest.raises(ValueError, match="allowlist"):
                LiveIngester(allowlist=[], **common)
            with pytest.raises(ValueError):
                LiveIngester(allowlist=[AllowlistEntry("", 5)], **common)
            with pytest.raises(ValueError):
                LiveIngester(allowlist=[AllowlistEntry("example.test", 0)], **common)
            with pytest.raises(ValueError):
                LiveIngester(allowlist=[AllowlistEntry("example.test", 5)], root=root, store=store, embedder=Embedder(),
                             chunk_size=8, overlap=8)
            with pytest.raises(ValueError):
                LiveIngester(allowlist=[AllowlistEntry("example.test", 5)], max_bytes=0, **common)
        finally:
            store.close()
