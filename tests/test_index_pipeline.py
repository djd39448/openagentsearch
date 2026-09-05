"""Post-roadmap #2: one raw HTML document becomes searchable through the real extractor, chunker,
an injected embedder, the real SQLite VectorStore and real cosine search - offline."""

import hashlib
import tempfile
from pathlib import Path

import pytest

from openagentsearch.chunk.chunker import chunk_text
from openagentsearch.extract.html import extract
from openagentsearch.pipeline.index import IndexReport, index_document, index_documents
from openagentsearch.vector.search import cosine_search
from openagentsearch.vector.store import VectorStore

TARGET = "ALPHA TARGET"
DOC = (
    "<html><head><title>Doc</title></head><body>"
    "<p>Ordinary introductory text sits here first.</p>"
    "<p>ALPHA TARGET appears only here.</p>"
    "</body></html>"
)
CHUNK_SIZE = 44
OVERLAP = 0


class StubEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0] if TARGET in text else [0.0, 1.0]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_index(chunk_id: str) -> int:
    return int(chunk_id.rsplit(":", 1)[1])


def test_index_document_creates_searchable_chunks():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        try:
            embedder = StubEmbedder()
            report = index_document(
                DOC, "https://example.test/doc", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP
            )
            assert isinstance(report, IndexReport)
            assert report.doc_sha256 == _sha(DOC)
            assert report.source_url == "https://example.test/doc"
            assert report.extracted_text_sha256 == _sha(extract(DOC)["text"])
            assert report.chunk_count == report.chunks_indexed == store.count() == len(report.chunk_ids)
            assert report.chunk_ids == tuple(f"{report.doc_sha256}:{i}" for i in range(report.chunk_count))
            records = store.load_all()
            assert {r["doc_sha256"] for r in records} == {report.doc_sha256}
            holders = [r for r in records if TARGET in r["text"]]
            assert len(holders) == 1 and holders[0]["chunk_id"] == f"{report.doc_sha256}:1"
            hits = cosine_search(store, embedder.embed(f"{TARGET} query"), k=store.count())
            assert hits[0][0] == f"{report.doc_sha256}:1"
            assert hits[0][1] == pytest.approx(1.0)
            assert TARGET in store.get(hits[0][0])["text"]
        finally:
            store.close()


def test_index_document_uses_extracted_visible_text():
    html = (
        "<html><head><style>FORBIDDEN_STYLE { color: red }</style>"
        "<script>var FORBIDDEN_SCRIPT = 1;</script></head>"
        "<body><nav>FORBIDDEN_NAV link</nav><p>VISIBLE ALLOWED PHRASE in the body.</p></body></html>"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        try:
            embedder = StubEmbedder()
            report = index_document(html, "u", store=store, embedder=embedder, chunk_size=64, overlap=8)
            records = sorted(store.load_all(), key=lambda r: _chunk_index(r["chunk_id"]))
            assert records and len(records) == report.chunks_indexed
            joined = " ".join(r["text"] for r in records)
            for forbidden in ("FORBIDDEN_STYLE", "FORBIDDEN_SCRIPT", "FORBIDDEN_NAV"):
                assert forbidden not in joined
            assert any("VISIBLE ALLOWED PHRASE" in r["text"] for r in records)
            assert embedder.calls == [r["text"] for r in records]
            assert [r["text"] for r in records] == [c["text"] for c in chunk_text(report.doc_sha256, extract(html)["text"], 64, 8)]
        finally:
            store.close()


def test_empty_extraction_writes_no_vectors():
    html = "<html><head><style>body{}</style><script>x()</script></head><body><nav>menu</nav></body></html>"
    assert extract(html)["text"] == ""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        try:
            embedder = StubEmbedder()
            report = index_document(html, "u", store=store, embedder=embedder, chunk_size=10, overlap=2)
            assert report.chunk_count == 0 and report.chunks_indexed == 0
            assert report.chunk_ids == ()
            assert report.extracted_text_sha256 == _sha("")
            assert store.count() == 0
            assert embedder.calls == []
        finally:
            store.close()


def test_index_documents_preserves_batch_order():
    first = "<html><body><p>First document about apples and orchards.</p></body></html>"
    second = "<html><body><p>Second document about ships and harbours and tides.</p></body></html>"
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        try:
            embedder = StubEmbedder()
            reports = index_documents(
                [(first, "https://a.test/1"), (second, "https://b.test/2")],
                store=store, embedder=embedder, chunk_size=16, overlap=4,
            )
            assert [r.source_url for r in reports] == ["https://a.test/1", "https://b.test/2"]
            assert [r.doc_sha256 for r in reports] == [_sha(first), _sha(second)]
            assert reports[0].doc_sha256 != reports[1].doc_sha256
            for report in reports:
                assert report.chunk_ids and all(cid.startswith(report.doc_sha256 + ":") for cid in report.chunk_ids)
                assert [_chunk_index(cid) for cid in report.chunk_ids] == list(range(report.chunks_indexed))
            assert store.count() == sum(r.chunks_indexed for r in reports)
            expected_calls = [
                c["text"] for html, report in ((first, reports[0]), (second, reports[1]))
                for c in chunk_text(report.doc_sha256, extract(html)["text"], 16, 4)
            ]
            assert embedder.calls == expected_calls
        finally:
            store.close()


def test_duplicate_document_fails_without_silent_reindex():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        try:
            embedder = StubEmbedder()
            report = index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            before = sorted(r["chunk_id"] for r in store.load_all())
            count_before, calls_before = store.count(), len(embedder.calls)
            assert before == sorted(report.chunk_ids) and count_before == report.chunks_indexed
            with pytest.raises(ValueError):
                index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert store.count() == count_before
            assert sorted(r["chunk_id"] for r in store.load_all()) == before  # no alternate ids, no duplicates
            assert len(embedder.calls) == calls_before + 1  # the embed preceding the first duplicate add is not rolled back
            with pytest.raises(ValueError):
                index_document(DOC, "   ", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            with pytest.raises(ValueError):
                index_document(b"<p>bytes</p>", "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert store.count() == report.chunks_indexed
        finally:
            store.close()
