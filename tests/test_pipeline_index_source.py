"""Coverage for the `already_indexed` refusal path of `index_source_document()` /
`index_source_documents()` (pipeline/index.py) -- distinct from `failed`, and previously never
exercised by any test in the A3 diff (every existing caller of these two functions used
all-distinct-content SourceDocs)."""

import tempfile
from pathlib import Path

import pytest

from openagentsearch.pipeline.index import index_source_document, index_source_documents
from openagentsearch.sources.base import SourceDoc
from openagentsearch.vector.store import VectorStore

CHUNK_KW = {"chunk_size": 2000, "overlap": 0}


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


def _doc(url: str, content: str) -> SourceDoc:
    return SourceDoc(
        url=url,
        kind="site",
        content=content,
        content_type="text",
        fetched_at=1700300000.0,
        provenance=(),
    )


def test_index_source_document_already_indexed_refusal_writes_nothing_and_reraises():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        embedder = _CountingEmbedder()
        try:
            doc = _doc("https://a.test/one", "Identical content shared across two URLs here.")
            first = index_source_document(doc, store=store, embedder=embedder, **CHUNK_KW)
            calls_after_first = len(embedder.calls)

            dupe = _doc("https://a.test/two", doc.content)  # same content -> same doc_sha256
            with pytest.raises(ValueError, match="already indexed"):
                index_source_document(dupe, store=store, embedder=embedder, **CHUNK_KW)

            # the refusal is raised BEFORE any embedding call -- the embedder is never re-invoked
            assert len(embedder.calls) == calls_after_first
            # the refusal writes no manifest row of its own: only the original "indexed" row exists
            entry = store.manifest_entry(first.doc_sha256)
            assert entry is not None and entry.status == "indexed" and entry.source_url == doc.url
            counts = store.manifest_counts()
            assert counts.indexed == 1 and counts.failed == 0
        finally:
            store.close()


def test_index_source_documents_counts_duplicate_content_as_already_indexed_not_failed():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
        embedder = _CountingEmbedder()
        try:
            content = "Batch-level duplicate content used for both docs in this test run."
            first = _doc("https://b.test/one", content)
            dupe = _doc("https://b.test/two", content)

            report = index_source_documents(
                [first, dupe], store=store, embedder=embedder, **CHUNK_KW
            )

            assert report.indexed == 1
            assert report.already_indexed == 1
            assert report.failed == 0
            # an already_indexed refusal is never recorded in `failures`, only `failed` docs are
            assert report.failures == ()
            assert len(report.reports) == 1
            assert store.manifest_counts().indexed == 1
        finally:
            store.close()
