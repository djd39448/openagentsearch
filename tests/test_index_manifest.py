"""Package A1: the index manifest - a SQLite table of per-document indexing outcomes
(`indexed` / `failed` / `superseded` / `refused`) that lives beside the vector rows and is
written in the same transaction as those rows. Real SQLite via VectorStore throughout."""

import hashlib
import sqlite3
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from openagentsearch.api.healthz import make_healthz_route
from openagentsearch.api.server import create_server
from openagentsearch.fetch.allowlist import AllowlistEntry
from openagentsearch.index.manifest import ManifestCorruptionError, ManifestCounts, ManifestEntry
from openagentsearch.pipeline.index import index_document, index_documents
from openagentsearch.pipeline.ingest import FetchResponse, LiveIngester
from openagentsearch.vector.store import VectorStore

DOC = (
    "<html><body><p>First paragraph of the document with enough words to chunk.</p>"
    "<p>Second paragraph continues the document with more distinct words.</p>"
    "<p>Third paragraph closes the document so three or more chunks exist.</p></body></html>"
)
DOC_V2 = (
    "<html><body><p>Rewritten first paragraph with quite different wording altogether now.</p>"
    "<p>Rewritten second paragraph continues with other distinct words entirely here.</p>"
    "</body></html>"
)
OTHER = (
    "<html><body><p>A different document about harbours, tides and ships at anchor.</p>"
    "</body></html>"
)
CHUNK_SIZE = 40
OVERLAP = 0
CHUNK_KW = {"chunk_size": CHUNK_SIZE, "overlap": OVERLAP}

PAGE_A = (
    "<html><head><title>A</title></head>"
    "<body><p>Alpha page about robots and crawling politely.</p></body></html>"
)
PAGE_B_SAME_TEXT = (
    "<html><head><title>B twin</title></head>"
    "<body><p>Alpha page about robots and crawling politely.</p></body></html>"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FailingEmbedder:
    """Embeds normally until call number `fail_on` (1-based), then raises."""

    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if len(self.calls) == self.fail_on:
            raise RuntimeError(f"embedder failed on call {self.fail_on}")
        return [1.0, 0.0]


class GoodEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.0, 1.0]


class _FakeFetcher:
    """Maps url -> FetchResponse or Exception; records every call. Never opens a socket."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        answer = self.responses.get(url, FetchResponse(404, "text/plain", b"missing", False))
        if isinstance(answer, Exception):
            raise answer
        return answer


def _html_response(body: str) -> FetchResponse:
    return FetchResponse(200, "text/html; charset=utf-8", body.encode("utf-8"), False)


def _store(tmpdir: str, name: str = "v.sqlite3") -> VectorStore:
    return VectorStore(Path(tmpdir) / name, 2)


def _index(store: VectorStore, html: str, url: str, embedder, **extra):
    """Shorthand for index_document() with this file's shared chunk_size/overlap."""
    return index_document(html, url, store=store, embedder=embedder, **CHUNK_KW, **extra)


# 1. Done-when: one indexed doc, one failed doc ------------------------------------------------


def test_indexed_and_failed_counts_and_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            good = GoodEmbedder()
            report = _index(store, DOC, "https://a.test/doc", good, indexed_at=1700000000.0)
            failing = FailingEmbedder(fail_on=2)
            with pytest.raises(RuntimeError, match="failed on call 2"):
                _index(store, OTHER, "https://b.test/doc", failing)

            counts = store.manifest_counts()
            assert counts == ManifestCounts(indexed=1, failed=1, superseded=0, refused=0)

            failed_sha = _sha(OTHER)
            failed_entry = store.manifest_entry(failed_sha)
            assert failed_entry is not None
            assert failed_entry.status == "failed"
            assert failed_entry.reason.startswith("RuntimeError:")
            # the failed doc has no vector rows: only the successfully indexed doc's chunks exist
            assert store.count() == report.chunk_count

            indexed_entry = store.manifest_entry(report.doc_sha256)
            assert indexed_entry is not None
            assert indexed_entry.chunk_count == report.chunk_count
            assert indexed_entry.extracted_sha256 == report.extracted_text_sha256
            assert indexed_entry.source_kind == "html"
            assert indexed_entry.indexed_at == 1700000000.0
        finally:
            store.close()


# 2. Same transaction: a rolled-back add_many rolls back its manifest row too ------------------


def test_add_many_manifest_row_rolls_back_with_the_vector_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            store.add("keep", "d0", [0.5, 0.5], "kept")
            entry = ManifestEntry(
                doc_sha256=_sha("some-doc"),
                source_url="https://a.test/1",
                status="indexed",
                reason="",
                indexed_at=1700000000.0,
                chunk_count=1,
                extracted_sha256="",
                source_kind="html",
            )
            with pytest.raises(ValueError, match="'keep' already exists"):
                store.add_many([("keep", "d1", [1.0, 1.0], "clash")], manifest=entry)
            assert store.manifest_entry(entry.doc_sha256) is None
        finally:
            store.close()


# 3. Superseded: reindexing the same URL supersedes the earlier indexed row --------------------


def test_superseded_on_reindex_at_same_url():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            url = "https://a.test/page"
            v1 = _index(store, DOC, url, GoodEmbedder())
            v2 = _index(store, DOC_V2, url, GoodEmbedder())
            assert v1.doc_sha256 != v2.doc_sha256

            v1_entry = store.manifest_entry(v1.doc_sha256)
            assert v1_entry is not None
            assert v1_entry.status == "superseded"
            assert v1_entry.reason == f"superseded by {v2.doc_sha256}"

            counts = store.manifest_counts()
            assert counts.indexed == 1 and counts.superseded == 1

            # v1's chunk rows are still present; superseding does not delete them
            assert store.existing_chunk_ids(list(v1.chunk_ids)) == set(v1.chunk_ids)
        finally:
            store.close()


# 4. Failed then indexed: the manifest row is upserted, not duplicated -------------------------


def test_failed_then_indexed_same_document_upserts():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            url = "https://a.test/doc"
            failing = FailingEmbedder(fail_on=1)
            with pytest.raises(RuntimeError):
                _index(store, DOC, url, failing)
            counts_after_failure = store.manifest_counts()
            expected_after_failure = ManifestCounts(indexed=0, failed=1, superseded=0, refused=0)
            assert counts_after_failure == expected_after_failure

            report = _index(store, DOC, url, GoodEmbedder())
            counts_after_success = store.manifest_counts()
            expected_after_success = ManifestCounts(indexed=1, failed=0, superseded=0, refused=0)
            assert counts_after_success == expected_after_success
            entry = store.manifest_entry(report.doc_sha256)
            assert entry is not None and entry.status == "indexed"
        finally:
            store.close()


# 5. Persistence: close and reopen the store on the same path ----------------------------------


def test_manifest_persists_across_reopen():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "v.sqlite3"
        store = VectorStore(path, 2)
        try:
            _index(store, DOC, "https://a.test/doc", GoodEmbedder())
            failing = FailingEmbedder(fail_on=1)
            with pytest.raises(RuntimeError):
                _index(store, OTHER, "https://b.test/doc", failing)
            before_counts = store.manifest_counts()
            before_entries = store.manifest_entries()
        finally:
            store.close()

        reopened = VectorStore(path, 2)
        try:
            assert reopened.manifest_counts() == before_counts
            assert reopened.manifest_entries() == before_entries
        finally:
            reopened.close()


# 6. /healthz over a real server ---------------------------------------------------------------


def test_store_aware_healthz_over_real_server():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            _index(store, DOC, "https://a.test/doc", GoodEmbedder())
            failing = FailingEmbedder(fail_on=1)
            with pytest.raises(RuntimeError):
                _index(store, OTHER, "https://b.test/doc", failing)
            counts = store.manifest_counts()
            assert counts == ManifestCounts(indexed=1, failed=1, superseded=0, refused=0)

            server = create_server("127.0.0.1", 0, routes={"/healthz": make_healthz_route(store)})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                url = f"http://127.0.0.1:{port}/healthz"
                with urllib.request.urlopen(url, timeout=5) as response:
                    body = response.read()
                expected = (
                    b'{"status":"ok","index":'
                    b'{"indexed":1,"failed":1,"superseded":0,"refused":0}}'
                )
                assert body == expected
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)

            # a server created WITHOUT the route still returns the plain default body
            default_server = create_server("127.0.0.1", 0)
            default_thread = threading.Thread(target=default_server.serve_forever, daemon=True)
            default_thread.start()
            try:
                default_port = default_server.server_address[1]
                default_url = f"http://127.0.0.1:{default_port}/healthz"
                with urllib.request.urlopen(default_url, timeout=5) as response:
                    default_body = response.read()
                assert default_body == b'{"status":"ok"}'
            finally:
                default_server.shutdown()
                default_server.server_close()
                default_thread.join(timeout=10)
        finally:
            store.close()


# 7. LiveIngester refused row: deduplicated page -----------------------------------------------


def test_live_ingester_records_a_refused_row_for_a_deduplicated_page():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "v.sqlite3", 2)
        try:
            robots_ok = FetchResponse(200, "text/plain", b"User-agent: *\nAllow: /\n", False)
            fetcher = _FakeFetcher({
                "http://example.test/robots.txt": robots_ok,
                "http://example.test/a.html": _html_response(PAGE_A),
                "http://example.test/b.html": _html_response(PAGE_B_SAME_TEXT),
            })
            ing = LiveIngester(
                root=root, allowlist=[AllowlistEntry("example.test", 5)], store=store,
                embedder=GoodEmbedder(), chunk_size=48, overlap=8, min_interval_s=0.0,
                fetcher=fetcher, sleep=lambda s: None,
            )
            a = ing.ingest("http://example.test/a.html")
            assert a.outcome == "indexed"
            b = ing.ingest("http://example.test/b.html")
            assert b.outcome == "deduplicated"

            entry = store.manifest_entry(b.doc_sha256)
            assert entry is not None
            assert entry.status == "refused"

            counts = store.manifest_counts()
            assert counts.indexed == 1 and counts.refused == 1
        finally:
            store.close()


# 8. Validation: bad status rejected on construction and on read-back --------------------------


def test_manifest_entry_rejects_invalid_status():
    with pytest.raises(ValueError):
        ManifestEntry(
            doc_sha256=_sha("x"), source_url="u", status="bogus", reason="", indexed_at=1.0,
            chunk_count=0, extracted_sha256="", source_kind="html",
        )


def test_manifest_entry_coerces_integer_timestamps_and_rejects_bool():
    entry = ManifestEntry(
        doc_sha256=_sha("x"), source_url="u", status="indexed", reason="", indexed_at=1700000000,
        chunk_count=0, extracted_sha256="", source_kind="html",
    )
    assert entry.indexed_at == 1700000000.0 and isinstance(entry.indexed_at, float)
    with pytest.raises(ValueError):
        ManifestEntry(
            doc_sha256=_sha("x"), source_url="u", status="indexed", reason="", indexed_at=True,
            chunk_count=0, extracted_sha256="", source_kind="html",
        )


def test_corrupt_status_row_raises_manifest_corruption_on_read():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "v.sqlite3"
        store = VectorStore(path, 2)
        try:
            side_conn = sqlite3.connect(path, timeout=5.0)
            try:
                with side_conn:
                    side_conn.execute(
                        "INSERT INTO manifest (doc_sha256, source_url, status, reason, "
                        "indexed_at, chunk_count, extracted_sha256, source_kind) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (_sha("y"), "https://a.test/x", "bogus", "", 1.0, 0, "", "html"),
                    )
            finally:
                side_conn.close()
            with pytest.raises(ManifestCorruptionError):
                store.manifest_counts()
        finally:
            store.close()


# 9. index_documents passes source_kind through ------------------------------------------------


def test_index_documents_passes_source_kind_through_to_manifest():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            reports = index_documents(
                [(DOC, "https://a.test/1"), (OTHER, "https://b.test/2")],
                store=store, embedder=GoodEmbedder(), source_kind="room", **CHUNK_KW,
            )
            for report in reports:
                assert report.source_kind == "room"
                entry = store.manifest_entry(report.doc_sha256)
                assert entry is not None and entry.source_kind == "room"
        finally:
            store.close()


# 10. Concurrency: a losing racer must not clobber the winner's indexed row --------------------


class _BarrierSyncedStore(VectorStore):
    """Test-only subclass: widens the TOCTOU window between the "not yet indexed" pre-check
    (existing_chunk_ids) and the write (add_many) so two threads racing on identical content both
    pass the pre-check before either reaches the write, deterministically reproducing the race
    instead of depending on incidental thread scheduling."""

    def __init__(self, *args, barrier: threading.Barrier, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._race_barrier = barrier

    def existing_chunk_ids(self, chunk_ids):  # type: ignore[override]
        result = super().existing_chunk_ids(chunk_ids)
        self._race_barrier.wait(timeout=10)
        return result


def test_concurrent_index_document_race_does_not_clobber_the_winners_indexed_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        barrier = threading.Barrier(2)
        store = _BarrierSyncedStore(Path(tmpdir) / "v.sqlite3", 2, barrier=barrier)
        try:
            errors: list[BaseException] = []

            def worker(url: str) -> None:
                try:
                    _index(store, DOC, url, GoodEmbedder())
                except Exception as exc:  # the losing racer is expected to raise
                    errors.append(exc)

            t1 = threading.Thread(target=worker, args=("https://a.test/one",))
            t2 = threading.Thread(target=worker, args=("https://b.test/two",))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            assert not t1.is_alive() and not t2.is_alive()

            # exactly one racer loses to the other's committed chunk ids
            assert len(errors) == 1
            assert "already exists" in str(errors[0])

            doc_sha = _sha(DOC)
            entry = store.manifest_entry(doc_sha)
            assert entry is not None
            assert entry.status == "indexed"
            assert store.manifest_counts() == ManifestCounts(
                indexed=1, failed=0, superseded=0, refused=0
            )
        finally:
            store.close()
