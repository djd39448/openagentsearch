"""Compose the existing extraction, chunking, embedding seam and vector store into one offline
indexing entry point.

Canonical flow, exactly:
    raw_html -> hashlib.sha256(raw_html UTF-8) -> extract(raw_html) -> chunk_text(doc_sha256,
    extracted["text"], chunk_size, overlap) -> embedder.embed(chunk["text"]) -> VectorStore.add(...)
and retrieval stays cosine_search(...). The pipeline chunk id is f"{doc_sha256}:{chunk_index}".

The embedder is injected by the caller (anything with embed(text) -> list[float]); no Ollama
client is constructed here, no network is touched, nothing is fetched. This module writes only
vector rows: raw HTML, extracted JSON, provenance and dedupe records are separate persistence
layers that this first cut does not wire in. Failures from extraction, chunking, embedding or the
store propagate unchanged; there is no partial-success flag, fallback or retry. index_documents()
is sequential and non-transactional: it stops at the first failure and leaves rows from earlier
documents in place.
"""

import hashlib
from dataclasses import dataclass
from typing import Iterable, Protocol

from openagentsearch.chunk.chunker import chunk_text
from openagentsearch.extract.html import extract
from openagentsearch.vector.store import VectorStore


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class IndexReport:
    doc_sha256: str
    source_url: str
    extracted_text_sha256: str
    chunk_count: int
    chunks_indexed: int
    chunk_ids: tuple[str, ...]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_document(
    raw_html: str, source_url: str, *, store: VectorStore, embedder: Embedder, chunk_size: int, overlap: int
) -> IndexReport:
    """Index one raw HTML document into `store`; returns what was written."""
    if not isinstance(raw_html, str) or not isinstance(source_url, str):
        raise ValueError("raw_html and source_url must be strings")
    if not source_url.strip():
        raise ValueError("source_url must be non-empty")
    doc_sha256 = _sha256(raw_html)
    extracted_text = extract(raw_html)["text"]
    extracted_text_sha256 = _sha256(extracted_text)
    chunks = chunk_text(doc_sha256, extracted_text, chunk_size, overlap)
    chunk_ids: list[str] = []
    for chunk in chunks:
        chunk_id = f"{doc_sha256}:{chunk['chunk_index']}"
        vector = embedder.embed(chunk["text"])
        store.add(chunk_id, doc_sha256, vector, chunk["text"])
        chunk_ids.append(chunk_id)
    return IndexReport(
        doc_sha256=doc_sha256,
        source_url=source_url,
        extracted_text_sha256=extracted_text_sha256,
        chunk_count=len(chunks),
        chunks_indexed=len(chunk_ids),
        chunk_ids=tuple(chunk_ids),
    )


def index_documents(
    documents: Iterable[tuple[str, str]], *, store: VectorStore, embedder: Embedder, chunk_size: int, overlap: int
) -> list[IndexReport]:
    """Index (raw_html, source_url) pairs in order; sequential, non-transactional, stops at the first failure."""
    reports: list[IndexReport] = []
    for item in documents:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("each document must be a (raw_html, source_url) tuple")
        raw_html, source_url = item
        reports.append(
            index_document(
                raw_html, source_url, store=store, embedder=embedder, chunk_size=chunk_size, overlap=overlap
            )
        )
    return reports
