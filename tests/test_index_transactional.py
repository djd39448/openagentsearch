"""Post-roadmap #6 (reviewer backlog item 2): per-document indexing is atomic. A document is either
fully indexed or absent - an embedder failure, a bad vector, or a chunk-id collision leaves the
SQLite file exactly as it was, and an already-indexed document is refused before any embedding."""

import hashlib
import tempfile
from pathlib import Path

import pytest

from openagentsearch.chunk.chunker import chunk_text
from openagentsearch.extract.html import extract
from openagentsearch.pipeline.index import index_document, index_documents
from openagentsearch.vector.store import VectorStore

DOC = (
    "<html><body><p>First paragraph of the document with enough words to chunk.</p>"
    "<p>Second paragraph continues the document with more distinct words.</p>"
    "<p>Third paragraph closes the document so three or more chunks exist.</p></body></html>"
)
OTHER = "<html><body><p>A different document about harbours, tides and ships at anchor.</p></body></html>"
CHUNK_SIZE = 40
OVERLAP = 0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _expected_chunks(html: str) -> list[dict]:
    return chunk_text(_sha(html), extract(html)["text"], CHUNK_SIZE, OVERLAP)


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


def _store(tmpdir: str) -> VectorStore:
    return VectorStore(Path(tmpdir) / "v.sqlite3", 2)


def test_embedder_failure_mid_document_writes_nothing():
    chunks = _expected_chunks(DOC)
    assert len(chunks) >= 3, "fixture must produce several chunks"
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            embedder = FailingEmbedder(fail_on=2)
            with pytest.raises(RuntimeError, match="failed on call 2"):
                index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert len(embedder.calls) == 2  # chunk 1 embedded, chunk 2 raised, chunk 3 never attempted
            assert store.count() == 0
            assert store.load_all() == []
            # the document is still indexable afterwards: no half-state blocks the retry
            good = GoodEmbedder()
            report = index_document(DOC, "u", store=store, embedder=good, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert report.chunks_indexed == len(chunks) == store.count()
        finally:
            store.close()


def test_all_embeddings_precede_the_first_write():
    """No row exists until every chunk has been embedded: a failure on the LAST chunk still writes nothing."""
    chunks = _expected_chunks(DOC)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            embedder = FailingEmbedder(fail_on=len(chunks))
            with pytest.raises(RuntimeError):
                index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert len(embedder.calls) == len(chunks)
            assert store.count() == 0
        finally:
            store.close()


def test_partial_prior_state_is_refused_before_any_embedding():
    """A stray row for one chunk id (simulating an old half-indexed document) blocks the whole document,
    costs zero embedder calls, and is left untouched."""
    doc_sha = _sha(DOC)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            store.add(f"{doc_sha}:1", doc_sha, [0.5, 0.5], "stale text")
            embedder = GoodEmbedder()
            with pytest.raises(ValueError, match="already indexed"):
                index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert embedder.calls == []
            assert store.count() == 1
            assert store.get(f"{doc_sha}:1")["text"] == "stale text"
        finally:
            store.close()


def test_fully_indexed_document_is_refused_without_embedding():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            embedder = GoodEmbedder()
            report = index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            calls_before = len(embedder.calls)
            with pytest.raises(ValueError, match="already indexed"):
                index_document(DOC, "u", store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP)
            assert len(embedder.calls) == calls_before
            assert store.count() == report.chunks_indexed
        finally:
            store.close()


def test_batch_keeps_earlier_documents_and_drops_the_failing_one_whole():
    first_chunks = _expected_chunks(OTHER)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            # OTHER indexes fully; DOC fails on its second chunk (call count continues across documents)
            embedder = FailingEmbedder(fail_on=len(first_chunks) + 2)
            with pytest.raises(RuntimeError):
                index_documents(
                    [(OTHER, "https://a.test/1"), (DOC, "https://b.test/2")],
                    store=store, embedder=embedder, chunk_size=CHUNK_SIZE, overlap=OVERLAP,
                )
            docs = {r["doc_sha256"] for r in store.load_all()}
            assert docs == {_sha(OTHER)}
            assert store.count() == len(first_chunks)
            doc_ids = [f"{_sha(DOC)}:{i}" for i in range(len(_expected_chunks(DOC)))]
            assert store.existing_chunk_ids(doc_ids) == set()
        finally:
            store.close()


def test_add_many_is_all_or_nothing_on_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            with pytest.raises(ValueError, match="does not match the configured dimension"):
                store.add_many([("a", "d", [1.0, 0.0], "t"), ("b", "d", [0.0, 1.0], "t"), ("c", "d", [1.0], "t")])
            assert store.count() == 0
            with pytest.raises(ValueError, match="boolean"):
                store.add_many([("a", "d", [1.0, 0.0], "t"), ("b", "d", [True, 1.0], "t")])
            assert store.count() == 0
            with pytest.raises(ValueError, match="repeated within the batch"):
                store.add_many([("a", "d", [1.0, 0.0], "t"), ("a", "d", [0.0, 1.0], "t")])
            assert store.count() == 0
            with pytest.raises(ValueError):
                store.add_many([("a", "d", [1.0, 0.0])])  # wrong arity
            assert store.count() == 0
            assert store.add_many([]) == 0
            assert store.count() == 0
        finally:
            store.close()


def test_add_many_rolls_back_on_collision_with_existing_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "v.sqlite3"
        store = VectorStore(path, 2)
        try:
            store.add("keep", "d0", [0.5, 0.5], "kept")
            batch = [("x1", "d1", [1.0, 0.0], "t1"), ("x2", "d1", [0.0, 1.0], "t2"), ("keep", "d1", [1.0, 1.0], "t3")]
            with pytest.raises(ValueError, match="'keep' already exists; no rows from the batch were written"):
                store.add_many(batch)
            assert store.count() == 1
            assert store.get("x1") is None and store.get("x2") is None
            assert store.get("keep")["text"] == "kept"
            assert store.add_many(batch[:2]) == 2
        finally:
            store.close()
        reopened = VectorStore(path, 2)
        try:
            assert sorted(r["chunk_id"] for r in reopened.load_all()) == ["keep", "x1", "x2"]
            assert reopened.get("x2")["vector"] == [0.0, 1.0]
        finally:
            reopened.close()


def test_existing_chunk_ids_handles_large_batches_and_bad_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        try:
            ids = [f"c{i}" for i in range(1200)]
            store.add_many([(cid, "d", [1.0, 0.0], "t") for cid in ids[::2]])
            assert store.existing_chunk_ids(ids) == set(ids[::2])
            assert store.existing_chunk_ids([]) == set()
            assert store.existing_chunk_ids(["missing"]) == set()
            with pytest.raises(ValueError):
                store.existing_chunk_ids([1])
        finally:
            store.close()


def test_closed_store_refuses_add_many():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir)
        store.close()
        with pytest.raises(RuntimeError, match="closed"):
            store.add_many([("a", "d", [1.0, 0.0], "t")])
        with pytest.raises(RuntimeError, match="closed"):
            store.existing_chunk_ids(["a"])
