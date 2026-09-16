"""The typed lexical index (`LexicalIndex`) and its on-disk JSON form (`lexical-v1.json`).

`to_json_bytes()` and `load_lexical_index()` are the two halves of one contract: anything
`to_json_bytes()` can produce, `load_lexical_index()` can read back to an equal `LexicalIndex`, and
anything `load_lexical_index()` accepts is exactly what `docs/static-index.md` documents -- no
tolerance for shapes this module doesn't itself write. See `handoff/C1-DESIGN.md` §3 for the field
layout and why it is frozen this way (the JavaScript reader in package C2b depends on it).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from openagentsearch.lexical.tokenize import TOKENIZER_ID

SCHEMA = "openagentsearch.lexical-index/1"


@dataclass(frozen=True)
class LexicalDoc:
    """One indexed document as the lexical index records it -- a lexical projection of the
    corresponding `flop-surface.jsonl` row, not a copy of it (field names and the `length`/`len`
    rename differ on purpose so the two files are never confused for one another)."""

    sha: str
    url: str
    title: str
    section: str
    kind: str
    abstract: str
    length: int


@dataclass(frozen=True)
class LexicalCounts:
    """Row/entry counts for one `LexicalIndex`, always present (0 when a count is zero)."""

    docs: int
    terms: int
    postings: int
    dropped_terms: int
    missing_extracted: int

    def as_dict(self) -> dict[str, int]:
        """JSON-shaped view, for the API edge -- pass the dataclass itself elsewhere."""
        return {
            "docs": self.docs,
            "terms": self.terms,
            "postings": self.postings,
            "dropped_terms": self.dropped_terms,
            "missing_extracted": self.missing_extracted,
        }


@dataclass(frozen=True)
class LexicalIndex:
    """A complete, self-describing BM25 lexical index: enough to rank a query against `docs`
    without recomputing anything from the source database.

    `docs` is ordered exactly like `flop-surface.jsonl` (`(kind, url, sha)`); a posting's
    `doc_index` is an index into this tuple. `terms` maps a token to its postings, each posting a
    `(doc_index, tf)` pair, postings sorted ascending by `doc_index`.

    NOT guaranteed: this is a snapshot as of `generated_at` against `db_sha256`, not a live view;
    a term with `df / N` over the build's `max_df_ratio` was dropped entirely and has no key here
    at all (see `counts.dropped_terms`) -- its absence from `terms` is not evidence the term never
    occurred in the corpus.
    """

    schema: str
    generated_at: str
    db_sha256: str
    tokenizer: str
    k1: float
    b: float
    avgdl: float
    docs: tuple[LexicalDoc, ...]
    terms: Mapping[str, tuple[tuple[int, int], ...]]
    counts: LexicalCounts


def to_json_bytes(index: LexicalIndex) -> bytes:
    """`index` -> its canonical UTF-8 JSON bytes: compact, `ensure_ascii=False`, keys sorted.

    Two `LexicalIndex` values that compare equal always produce identical bytes here (and the
    reverse holds for `load_lexical_index()`), which is what makes two builds of the same input
    byte-identical -- see `openagentsearch.lexical.build.build_lexical_index`.
    """
    obj: dict[str, object] = {
        "schema": index.schema,
        "generated_at": index.generated_at,
        "db_sha256": index.db_sha256,
        "tokenizer": index.tokenizer,
        "bm25": {"k1": index.k1, "b": index.b},
        "avgdl": index.avgdl,
        "docs": [
            {
                "sha": doc.sha,
                "url": doc.url,
                "title": doc.title,
                "section": doc.section,
                "kind": doc.kind,
                "abstract": doc.abstract,
                "len": doc.length,
            }
            for doc in index.docs
        ],
        "terms": {
            term: [[doc_index, tf] for doc_index, tf in postings]
            for term, postings in index.terms.items()
        },
        "counts": index.counts.as_dict(),
    }
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _fail(problem: str) -> ValueError:
    return ValueError(f"malformed lexical index: {problem}")


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string, got {value!r}")
    return value


def _require_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{name} must be a number, got {value!r}")
    return float(value)


def _require_nonneg_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _parse_docs(raw_docs: object) -> tuple[LexicalDoc, ...]:
    if not isinstance(raw_docs, list):
        raise _fail(f"docs must be a list, got {type(raw_docs).__name__}")
    doc_str_fields = ("sha", "url", "title", "section", "kind", "abstract")
    docs: list[LexicalDoc] = []
    for i, raw_doc in enumerate(raw_docs):
        if not isinstance(raw_doc, dict):
            raise _fail(f"docs[{i}] must be an object, got {type(raw_doc).__name__}")
        for field in (*doc_str_fields, "len"):
            if field not in raw_doc:
                raise _fail(f"docs[{i}] is missing {field!r}")
        docs.append(
            LexicalDoc(
                sha=_require_str(raw_doc["sha"], f"docs[{i}].sha"),
                url=_require_str(raw_doc["url"], f"docs[{i}].url"),
                title=_require_str(raw_doc["title"], f"docs[{i}].title"),
                section=_require_str(raw_doc["section"], f"docs[{i}].section"),
                kind=_require_str(raw_doc["kind"], f"docs[{i}].kind"),
                abstract=_require_str(raw_doc["abstract"], f"docs[{i}].abstract"),
                length=_require_nonneg_int(raw_doc["len"], f"docs[{i}].len"),
            )
        )
    return tuple(docs)


def _parse_terms(
    raw_terms: object, doc_count: int
) -> dict[str, tuple[tuple[int, int], ...]]:
    if not isinstance(raw_terms, dict):
        raise _fail(f"terms must be an object, got {type(raw_terms).__name__}")
    terms: dict[str, tuple[tuple[int, int], ...]] = {}
    for term, raw_postings in raw_terms.items():
        if not isinstance(term, str):
            raise _fail(f"a terms key must be a string, got {term!r}")
        if not isinstance(raw_postings, list):
            raise _fail(f"terms[{term!r}] must be a list, got {type(raw_postings).__name__}")
        parsed: list[tuple[int, int]] = []
        previous_doc_index = -1
        for j, posting in enumerate(raw_postings):
            is_pair = (
                isinstance(posting, list)
                and len(posting) == 2
                and not isinstance(posting[0], bool)
                and isinstance(posting[0], int)
                and not isinstance(posting[1], bool)
                and isinstance(posting[1], int)
            )
            if not is_pair:
                raise _fail(f"terms[{term!r}][{j}] must be a [doc_index, tf] pair of integers")
            doc_index, tf = posting[0], posting[1]
            if not (0 <= doc_index < doc_count):
                raise _fail(
                    f"terms[{term!r}][{j}] doc_index {doc_index} is out of range for "
                    f"{doc_count} docs"
                )
            if tf < 0:
                raise _fail(f"terms[{term!r}][{j}] tf must be non-negative, got {tf}")
            if doc_index <= previous_doc_index:
                raise _fail(f"terms[{term!r}] postings are not sorted by doc_index at index {j}")
            previous_doc_index = doc_index
            parsed.append((doc_index, tf))
        terms[term] = tuple(parsed)
    return terms


def load_lexical_index(path: Path, *, max_bytes: int = 24 * 1024 * 1024) -> LexicalIndex:
    """Fail-closed load of a `lexical-v1.json` file from `path`.

    Raises `ValueError` naming the FIRST problem found, and never returns a partially-built
    `LexicalIndex`, for: a file over `max_bytes`; a top level that is not a JSON object; a
    `schema` other than `SCHEMA`; a `tokenizer` other than `TOKENIZER_ID`; a malformed `docs`
    entry (wrong type, a missing field); a `terms` posting with a `doc_index` outside
    `range(len(docs))`, a negative `tf`, a non-integer element, or postings not strictly sorted
    by `doc_index`.

    NOT guaranteed: this does not re-derive `avgdl` or `counts` from `docs`/`terms` to check they
    are internally consistent, and it does not verify `db_sha256` against any actual database file
    -- a hand-edited file with self-consistent-looking but wrong aggregate numbers loads without
    complaint.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise _fail(f"file is {size} bytes, over the {max_bytes} byte limit")
    raw = path.read_bytes()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(f"not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise _fail(f"top level must be a JSON object, got {type(obj).__name__}")

    schema = _require_str(obj.get("schema"), "schema")
    if schema != SCHEMA:
        raise _fail(f"schema must be {SCHEMA!r}, got {schema!r}")
    tokenizer = _require_str(obj.get("tokenizer"), "tokenizer")
    if tokenizer != TOKENIZER_ID:
        raise _fail(f"tokenizer must be {TOKENIZER_ID!r}, got {tokenizer!r}")

    generated_at = _require_str(obj.get("generated_at"), "generated_at")
    db_sha256 = _require_str(obj.get("db_sha256"), "db_sha256")

    bm25 = obj.get("bm25")
    if not isinstance(bm25, dict):
        raise _fail(f"bm25 must be an object, got {type(bm25).__name__}")
    k1 = _require_number(bm25.get("k1"), "bm25.k1")
    b = _require_number(bm25.get("b"), "bm25.b")
    avgdl = _require_number(obj.get("avgdl"), "avgdl")

    docs = _parse_docs(obj.get("docs"))
    terms = _parse_terms(obj.get("terms"), len(docs))

    raw_counts = obj.get("counts")
    if not isinstance(raw_counts, dict):
        raise _fail(f"counts must be an object, got {type(raw_counts).__name__}")
    counts = LexicalCounts(
        docs=_require_nonneg_int(raw_counts.get("docs"), "counts.docs"),
        terms=_require_nonneg_int(raw_counts.get("terms"), "counts.terms"),
        postings=_require_nonneg_int(raw_counts.get("postings"), "counts.postings"),
        dropped_terms=_require_nonneg_int(raw_counts.get("dropped_terms"), "counts.dropped_terms"),
        missing_extracted=_require_nonneg_int(
            raw_counts.get("missing_extracted"), "counts.missing_extracted"
        ),
    )

    return LexicalIndex(
        schema=schema,
        generated_at=generated_at,
        db_sha256=db_sha256,
        tokenizer=tokenizer,
        k1=k1,
        b=b,
        avgdl=avgdl,
        docs=docs,
        terms=terms,
        counts=counts,
    )
