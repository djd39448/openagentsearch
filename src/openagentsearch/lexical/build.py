"""Builds a `LexicalIndex` from an already-open `VectorStore` and the same `root` directory
`pipeline.publish` reads extracted text from, and writes it to disk atomically.

Deliberately mirrors `pipeline.publish.build_static_index()`'s own choices -- the same manifest
rows (`status == "indexed"`), the same `(kind, url, sha)` ordering, the same extracted-record
reader and the same abstract cut -- imported from there rather than recomputed here, so the
lexical index's `docs` and `flop-surface.jsonl` never silently drift apart. See
`handoff/C1-DESIGN.md` §2-3 for why this is a separate file rather than embedded in `publish`.
"""

import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from openagentsearch.index.manifest import ManifestEntry
from openagentsearch.lexical.index import (
    SCHEMA,
    LexicalCounts,
    LexicalDoc,
    LexicalIndex,
    to_json_bytes,
)
from openagentsearch.lexical.tokenize import MAX_INPUT_CHARS, TOKENIZER_ID, tokenize
from openagentsearch.pipeline.publish import _make_abstract, _read_extracted
from openagentsearch.vector.store import VectorStore

# Matches the abstract length pipeline.publish.build_static_index() uses by default, and the
# "300-char abstract" C1-DESIGN §1 documents as the Worker's /search "snippet".
_ABSTRACT_CHARS = 300


@dataclass(frozen=True)
class LexicalBuildReport:
    """What one `build_lexical_index()` call computed -- the same numbers `LexicalIndex.counts`
    carries, plus `seconds`, the wall-clock time the build itself took (unrelated to
    `generated_at`, which is a value recorded IN the index, not measured by this build)."""

    docs: int
    terms: int
    postings: int
    dropped_terms: int
    missing_extracted: int
    seconds: float


def _iso8601_utc(timestamp: float) -> str:
    """`timestamp` (Unix epoch seconds) as a `Z`-suffixed ISO-8601 string in UTC, always at
    whole-second precision -- the same convention `pipeline.publish` uses for `generated_at`."""
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def build_lexical_index(
    store: VectorStore,
    root: Path,
    *,
    k1: float = 1.2,
    b: float = 0.75,
    max_df_ratio: float = 0.5,
    abstract_chars: int = _ABSTRACT_CHARS,
    generated_at: float | None = None,
    clock: Callable[[], float] = time.time,
    entries: Sequence[ManifestEntry] | None = None,
    db_sha256: str | None = None,
) -> tuple[LexicalIndex, LexicalBuildReport]:
    """Read `store`'s manifest and `root/extracted/` and build one `LexicalIndex` in memory
    (nothing is written to disk here -- see `write_lexical_index`).

    Only manifest rows with `status == "indexed"` become docs, ordered by
    `(source_kind, source_url, doc_sha256)` -- the same order and the same rows
    `pipeline.publish.build_static_index()` puts in `flop-surface.jsonl`. Per doc: `title` and
    `text` come from `root/extracted/<sha>.json` through `pipeline.publish`'s own reader; a
    missing or unreadable record yields `title=text=""` and is counted in
    `LexicalBuildReport.missing_extracted`, but the doc still appears (with whatever tokens its
    URL fragment alone contributes, ordinarily none). `section` is the URL's fragment (`""` when
    absent) and `abstract` is `flop-surface.jsonl`'s own abstract cut, `_make_abstract(text,
    abstract_chars)` -- `abstract_chars` defaults to the same 300 `pipeline.publish` defaults to,
    but a caller (in particular `build_static_index()`) that used a different `abstract_chars`
    for the surface file must pass the same value here for the two files' abstracts to agree. The
    indexed text is `title + "\\n" + section + "\\n" + text`, bounded to `tokenize()`'s own
    `MAX_INPUT_CHARS` (truncated, not refused, so one oversized document can never abort the
    whole build -- see `LexicalBuildReport`); `length` is its (possibly truncated) token count.

    A term is dropped from the index (and counted in `LexicalBuildReport.dropped_terms`) when it
    appears in more than `max_df_ratio` of docs (`df / N > max_df_ratio`, strict) -- with zero
    docs nothing is dropped, since there is nothing to compute a ratio over. `avgdl` is the mean
    doc `length` (`0.0` for zero docs). `generated_at` is `clock()` unless a value is given
    explicitly -- passing the same `generated_at` across two calls against the same store/root is
    what makes `to_json_bytes()` of the two results byte-identical.

    `entries` and `db_sha256` let a caller that has ALREADY read the manifest and hashed the store
    (in particular `build_static_index()`, which needs its own copies for `manifest.json`) pass
    those exact values through instead of this function reading/hashing them again -- so
    `lexical-v1.json` is provably built from the same snapshot as `manifest.json` and
    `flop-surface.jsonl`, not a second, independently-read one that could disagree if the store
    changed in between (it is safe to open concurrently -- see `VectorStore`). Leave both `None`
    (the default) for a standalone build, which reads/hashes them itself.

    NOT guaranteed: this is a snapshot, not a live view -- a manifest row written after `entries`
    was read (whether by this call or by the caller that passed it in) is not reflected in the
    result. `root` is trusted to be the same root the crawl/indexing pipeline wrote `extracted/`
    under.
    """
    started = time.perf_counter()
    root = Path(root)

    all_entries = store.manifest_entries() if entries is None else entries
    indexed = sorted(
        (entry for entry in all_entries if entry.status == "indexed"),
        key=lambda entry: (entry.source_kind, entry.source_url, entry.doc_sha256),
    )

    docs: list[LexicalDoc] = []
    doc_tokens: list[list[str]] = []
    missing_extracted = 0
    for entry in indexed:
        title, text, found = _read_extracted(root, entry.doc_sha256)
        if not found:
            missing_extracted += 1
        section = urlsplit(entry.source_url).fragment
        abstract = _make_abstract(text, abstract_chars) if found else ""
        indexed_text = f"{title}\n{section}\n{text}"
        if len(indexed_text) > MAX_INPUT_CHARS:
            indexed_text = indexed_text[:MAX_INPUT_CHARS]
        tokens = tokenize(indexed_text)
        doc_tokens.append(tokens)
        docs.append(
            LexicalDoc(
                sha=entry.doc_sha256,
                url=entry.source_url,
                title=title,
                section=section,
                kind=entry.source_kind,
                abstract=abstract,
                length=len(tokens),
            )
        )

    doc_count = len(docs)
    term_freq_by_doc: list[dict[str, int]] = []
    doc_freq: dict[str, int] = {}
    for tokens in doc_tokens:
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        term_freq_by_doc.append(tf)
        for token in tf:
            doc_freq[token] = doc_freq.get(token, 0) + 1

    terms: dict[str, tuple[tuple[int, int], ...]] = {}
    dropped_terms = 0
    postings_total = 0
    for term in sorted(doc_freq):
        df = doc_freq[term]
        if doc_count > 0 and (df / doc_count) > max_df_ratio:
            dropped_terms += 1
            continue
        postings = tuple(
            (doc_index, tf[term])
            for doc_index, tf in enumerate(term_freq_by_doc)
            if term in tf
        )
        terms[term] = postings
        postings_total += len(postings)

    avgdl = (sum(doc.length for doc in docs) / doc_count) if doc_count else 0.0
    resolved_db_sha256 = (
        hashlib.sha256(store.path.read_bytes()).hexdigest() if db_sha256 is None else db_sha256
    )
    when = clock() if generated_at is None else generated_at

    index = LexicalIndex(
        schema=SCHEMA,
        generated_at=_iso8601_utc(when),
        db_sha256=resolved_db_sha256,
        tokenizer=TOKENIZER_ID,
        k1=k1,
        b=b,
        avgdl=avgdl,
        docs=tuple(docs),
        terms=terms,
        counts=LexicalCounts(
            docs=doc_count,
            terms=len(terms),
            postings=postings_total,
            dropped_terms=dropped_terms,
            missing_extracted=missing_extracted,
        ),
    )
    report = LexicalBuildReport(
        docs=doc_count,
        terms=len(terms),
        postings=postings_total,
        dropped_terms=dropped_terms,
        missing_extracted=missing_extracted,
        seconds=time.perf_counter() - started,
    )
    return index, report


class LexicalSizeError(ValueError):
    """Raised by `write_lexical_index()` when the serialized index exceeds `max_bytes` --
    raised BEFORE any file (not even a temp file) is created."""


def write_lexical_index(
    index: LexicalIndex, out_path: Path, *, max_bytes: int = 24 * 1024 * 1024
) -> int:
    """Serialize `index` (see `openagentsearch.lexical.index.to_json_bytes`) and write it to
    `out_path` atomically (temp file in the same directory, then `os.replace`), returning the
    byte count written.

    Raises:
        LexicalSizeError: the serialized bytes exceed `max_bytes`. Checked BEFORE any write, so a
            refusal leaves `out_path`'s directory exactly as it was found.
    """
    data = to_json_bytes(index)
    if len(data) > max_bytes:
        raise LexicalSizeError(
            f"lexical index is {len(data)} bytes, over the {max_bytes} byte limit"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(data)
