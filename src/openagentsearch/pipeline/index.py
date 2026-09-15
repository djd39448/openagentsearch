"""Compose the existing extraction, chunking, embedding seam and vector store into one offline
indexing entry point.

Canonical flow, exactly:
    raw_html -> hashlib.sha256(raw_html UTF-8) -> extract(raw_html) -> chunk_text(doc_sha256,
    extracted["text"], chunk_size, overlap) -> embedder.embed(chunk["text"]) for EVERY chunk ->
    VectorStore.add_many(all rows, manifest=entry) in one transaction
and retrieval stays cosine_search(...). The pipeline chunk id is f"{doc_sha256}:{chunk_index}".

Per-document atomicity: a document is either fully indexed (every chunk row present) or absent.
Embeddings are computed for all chunks before anything is written, and the write is a single
SQLite transaction, so an embedder failure on chunk N or a store failure leaves no rows for the
document. A document whose chunk ids already exist in the store is refused BEFORE any embedding
call (ValueError); it is never partially rewritten and never silently re-indexed.

The embedder is injected by the caller (anything with embed(text) -> list[float]); no Ollama
client is constructed here, no network is touched, nothing is fetched. This module writes vector
rows and one index-manifest row per document: raw HTML, extracted JSON, provenance and dedupe
records remain separate persistence layers that this module does not wire in. Failures from
extraction, chunking, embedding or the store propagate unchanged after being recorded as a
`failed` manifest row (best-effort: a failure while recording the failure itself never masks the
original exception); there is no partial-success flag, fallback or retry. The "already indexed"
refusal is not a failure and writes nothing to the manifest. index_documents() is sequential: each
document is atomic on its own, it stops at the first failure, and documents indexed earlier in the
batch stay indexed (the batch as a whole is not one transaction).
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from openagentsearch.chunk.chunker import chunk_text
from openagentsearch.extract.html import extract
from openagentsearch.extract.store import ExtractStore
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.index.manifest import ManifestEntry
from openagentsearch.sources.base import SourceDoc
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
    source_kind: str
    indexed_at: float


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def index_document(
    raw_html: str,
    source_url: str,
    *,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    source_kind: str = "html",
    indexed_at: float | None = None,
) -> IndexReport:
    """Index one raw HTML document into `store` atomically; returns what was written.

    Order of operations: validate -> extract -> chunk -> refuse if any chunk id already exists ->
    embed every chunk (no writes yet) -> write all rows and the manifest entry in one transaction.

    `indexed_at` is the manifest timestamp to record; `None` (the default) means "now", read
    once at write time via `time.time()`. A failure anywhere from extract() through the final
    write is recorded as a `failed` manifest row (chunk_count and extracted_sha256 reflect
    whatever was known at the point of failure; 0 / "" if extraction itself never completed) and
    then the original exception is re-raised unchanged — a failure while recording the failure
    never masks it. The "already indexed" refusal (raised before any embedding call) is not a
    failure: it writes nothing to the manifest.
    """
    if not isinstance(raw_html, str) or not isinstance(source_url, str):
        raise ValueError("raw_html and source_url must be strings")
    if not source_url.strip():
        raise ValueError("source_url must be non-empty")
    if not isinstance(source_kind, str) or not source_kind.strip():
        raise ValueError("source_kind must be non-empty")
    doc_sha256 = _sha256(raw_html)
    chunk_count_known = 0
    extracted_sha256_known = ""
    try:
        extracted_text = extract(raw_html)["text"]
        extracted_sha256_known = _sha256(extracted_text)
        chunks = chunk_text(doc_sha256, extracted_text, chunk_size, overlap)
        chunk_count_known = len(chunks)
        chunk_ids = [f"{doc_sha256}:{chunk['chunk_index']}" for chunk in chunks]
        already = store.existing_chunk_ids(chunk_ids)
        if already:
            raise ValueError(
                f"document {doc_sha256} is already indexed "
                f"({len(already)} of {len(chunk_ids)} chunk ids exist); "
                "nothing was embedded or written"
            )
        rows = []
        for chunk_id, chunk in zip(chunk_ids, chunks):
            vector = embedder.embed(chunk["text"])
            rows.append((chunk_id, doc_sha256, vector, chunk["text"]))
        write_time = time.time() if indexed_at is None else indexed_at
        entry = ManifestEntry(
            doc_sha256=doc_sha256,
            source_url=source_url,
            status="indexed",
            reason="",
            indexed_at=write_time,
            chunk_count=len(chunks),
            extracted_sha256=extracted_sha256_known,
            source_kind=source_kind,
        )
        written = store.add_many(rows, manifest=entry)
    except Exception as exc:
        if isinstance(exc, ValueError) and "already indexed" in str(exc):
            raise
        try:
            store.record_manifest(
                ManifestEntry(
                    doc_sha256=doc_sha256,
                    source_url=source_url,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                    indexed_at=time.time() if indexed_at is None else indexed_at,
                    chunk_count=chunk_count_known,
                    extracted_sha256=extracted_sha256_known,
                    source_kind=source_kind,
                )
            )
        except Exception:
            pass  # never let a secondary failure mask the original exception below
        raise
    return IndexReport(
        doc_sha256=doc_sha256,
        source_url=source_url,
        extracted_text_sha256=extracted_sha256_known,
        chunk_count=len(chunks),
        chunks_indexed=written,
        chunk_ids=tuple(chunk_ids),
        source_kind=source_kind,
        indexed_at=write_time,
    )


@dataclass(frozen=True)
class SourceIndexReport:
    """Outcome of one `index_source_documents()` batch."""

    indexed: int
    already_indexed: int
    failed: int
    reports: tuple[IndexReport, ...]
    failures: tuple[tuple[str, str], ...]


def index_source_document(
    doc: SourceDoc,
    *,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    root: Path | None = None,
    indexed_at: float | None = None,
) -> IndexReport:
    """Index one already-fetched `SourceDoc` (from any `openagentsearch.sources.*` adapter) into
    `store`, atomically, the same way `index_document()` indexes raw HTML.

    Content-type aware: an `html` doc is run through the same `extract()` path `index_document()`
    uses (`text`/`title`/`lang` come from the HTML extractor). A `text` doc is indexed as-is
    (`doc.content` IS the text to chunk); its extracted `title` is `doc.title` when set, otherwise
    the first line of `doc.content`, and its `lang` is recorded as `"und"` (undetermined -- no
    language detection runs over plain text here). `doc_sha256` is `sha256(doc.content utf-8)`,
    `source_url` is `doc.url`, and `source_kind` is `doc.kind`; the manifest is written exactly as
    in `index_document()` (indexed / failed, same atomicity, same "already indexed" refusal before
    any embedding call).

    When `root` is given, the raw bytes and an extracted-document JSON are ALSO written, so that
    `/doc/<sha256>` can serve this document: `RawStore(root).put(doc.url,
    doc.content.encode("utf-8"), status, True, doc.fetched_at)` (status is read out of
    `doc.provenance_dict()["status"]` when the adapter recorded one, else `0` -- this is how a
    real fetch is distinguished from a file-derived document that was never itself an HTTP
    response) followed by `ExtractStore(root).put(doc_sha256, doc.url, {"text", "title", "lang"},
    indexed_at)`. Those two writes happen AFTER the "already indexed" refusal check (so a refusal
    writes nothing at all, matching `index_document()`) but BEFORE embedding, and -- exactly like
    `LiveIngester` -- they are KEPT even when a later step (embedding, the vector-store write)
    then fails: they are evidence of what was seen, not a promise that indexing then succeeded.

    `indexed_at` is read once ("now" via `time.time()` when `None`) and used for both the
    `ExtractStore` write and the final manifest row, the same "read once at write time" contract
    `index_document()` documents. A failure anywhere after the "already indexed" check is recorded
    as a `failed` manifest row before the original exception is re-raised unchanged, same as
    `index_document()`.

    NOT guaranteed: this function does not decide whether `doc` is trustworthy or safe to index --
    that is the calling adapter's responsibility. It does not deduplicate against previously-seen
    extracted text the way `LiveIngester`/`DedupingExtractStore` does; indexing the same text twice
    under two different URLs produces two manifest rows.
    """
    if not isinstance(doc, SourceDoc):
        raise ValueError("doc must be a SourceDoc")
    doc_sha256 = _sha256(doc.content)
    chunk_count_known = 0
    extracted_sha256_known = ""
    try:
        if doc.content_type == "html":
            extracted = extract(doc.content)
            text = extracted["text"]
            extract_title = extracted["title"]
            lang = extracted["lang"]
        else:
            text = doc.content
            first_line = next(iter(doc.content.splitlines()), doc.content)
            extract_title = doc.title if doc.title else first_line
            lang = "und"
        extracted_sha256_known = _sha256(text)
        chunks = chunk_text(doc_sha256, text, chunk_size, overlap)
        chunk_count_known = len(chunks)
        chunk_ids = [f"{doc_sha256}:{chunk['chunk_index']}" for chunk in chunks]
        already = store.existing_chunk_ids(chunk_ids)
        if already:
            raise ValueError(
                f"document {doc_sha256} is already indexed "
                f"({len(already)} of {len(chunk_ids)} chunk ids exist); "
                "nothing was embedded or written"
            )
        write_time = time.time() if indexed_at is None else indexed_at
        if root is not None:
            provenance = doc.provenance_dict()
            status = int(provenance["status"]) if "status" in provenance else 0
            body = doc.content.encode("utf-8")
            RawStore(root).put(doc.url, body, status, True, doc.fetched_at)
            extracted_record = {"text": text, "title": extract_title, "lang": lang}
            ExtractStore(root).put(doc_sha256, doc.url, extracted_record, write_time)
        rows = []
        for chunk_id, chunk in zip(chunk_ids, chunks):
            vector = embedder.embed(chunk["text"])
            rows.append((chunk_id, doc_sha256, vector, chunk["text"]))
        entry = ManifestEntry(
            doc_sha256=doc_sha256,
            source_url=doc.url,
            status="indexed",
            reason="",
            indexed_at=write_time,
            chunk_count=len(chunks),
            extracted_sha256=extracted_sha256_known,
            source_kind=doc.kind,
        )
        written = store.add_many(rows, manifest=entry)
    except Exception as exc:
        if isinstance(exc, ValueError) and "already indexed" in str(exc):
            raise
        try:
            store.record_manifest(
                ManifestEntry(
                    doc_sha256=doc_sha256,
                    source_url=doc.url,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                    indexed_at=time.time() if indexed_at is None else indexed_at,
                    chunk_count=chunk_count_known,
                    extracted_sha256=extracted_sha256_known,
                    source_kind=doc.kind,
                )
            )
        except Exception:
            pass  # never let a secondary failure mask the original exception below
        raise
    return IndexReport(
        doc_sha256=doc_sha256,
        source_url=doc.url,
        extracted_text_sha256=extracted_sha256_known,
        chunk_count=len(chunks),
        chunks_indexed=written,
        chunk_ids=tuple(chunk_ids),
        source_kind=doc.kind,
        indexed_at=write_time,
    )


def index_source_documents(
    docs: Iterable[SourceDoc],
    *,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    root: Path | None = None,
    indexed_at: float | None = None,
) -> SourceIndexReport:
    """Index `SourceDoc`s in order via `index_source_document()`. Sequential, and each document is
    atomic on its own, but UNLIKE `index_documents()` a failure does NOT stop the batch: every
    document is attempted, `already_indexed` refusals are counted separately from `failed`
    documents (and never appear in `failures`), and `failures` carries `(url, reason)` for every
    document whose indexing raised something other than the "already indexed" refusal.

    NOT guaranteed: `reports` (successfully indexed documents only) is not the same length as
    `docs`; use `indexed + already_indexed + failed` against the count of `docs` consumed to
    account for all of them.
    """
    reports: list[IndexReport] = []
    failures: list[tuple[str, str]] = []
    already_indexed = 0
    failed = 0
    for doc in docs:
        try:
            reports.append(
                index_source_document(
                    doc,
                    store=store,
                    embedder=embedder,
                    chunk_size=chunk_size,
                    overlap=overlap,
                    root=root,
                    indexed_at=indexed_at,
                )
            )
        except Exception as exc:
            if isinstance(exc, ValueError) and "already indexed" in str(exc):
                already_indexed += 1
            else:
                failed += 1
                failures.append((doc.url, f"{type(exc).__name__}: {exc}"))
    return SourceIndexReport(
        indexed=len(reports),
        already_indexed=already_indexed,
        failed=failed,
        reports=tuple(reports),
        failures=tuple(failures),
    )


def index_documents(
    documents: Iterable[tuple[str, str]],
    *,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    source_kind: str = "html",
) -> list[IndexReport]:
    """Index (raw_html, source_url) pairs in order. Each document is atomic on its own; the batch
    is sequential, stops at the first failure, and keeps the documents already indexed before it."""
    reports: list[IndexReport] = []
    for item in documents:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("each document must be a (raw_html, source_url) tuple")
        raw_html, source_url = item
        reports.append(
            index_document(
                raw_html,
                source_url,
                store=store,
                embedder=embedder,
                chunk_size=chunk_size,
                overlap=overlap,
                source_kind=source_kind,
            )
        )
    return reports
