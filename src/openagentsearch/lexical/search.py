"""The reference BM25 ranking over a `LexicalIndex` -- the golden implementation package C2b's
JavaScript Worker must reproduce exactly. `tests/fixtures/lexical/queries.json` (20 queries, this
function's own output) is the contract both suites replay against.

Pure: `search()` touches no I/O and has no side effects, so it can be called once per request with
no caching beyond whatever the caller does with an already-loaded `LexicalIndex`.
"""

import math
from dataclasses import dataclass

from openagentsearch.lexical.index import LexicalDoc, LexicalIndex
from openagentsearch.lexical.tokenize import query_terms

_MIN_K = 1
_MAX_K = 50


@dataclass(frozen=True)
class SearchHit:
    """One ranked result. `snippet` is always the doc's `abstract` verbatim -- never a
    query-highlighted excerpt."""

    sha: str
    url: str
    title: str
    section: str
    kind: str
    score: float
    snippet: str


def _bm25_term_score(idf: float, tf: int, k1: float, b: float, length_ratio: float) -> float:
    """One term's contribution to one doc's score. `length_ratio` is `doc.length / avgdl`, with
    the caller responsible for treating `avgdl == 0` as a ratio of `0` (see `search`)."""
    denom = tf + k1 * (1 - b + b * length_ratio)
    return idf * tf * (k1 + 1) / denom


def search(
    index: LexicalIndex,
    q: str,
    k: int,
    *,
    kind: str | None = None,
    max_terms: int = 32,
) -> list[SearchHit]:
    """Rank `index.docs` against `q` by BM25 (`index.k1`, `index.b`) and return the top `k`.

    `q` is reduced to at most `max_terms` distinct terms via `query_terms`; only terms present in
    `index.terms` (a term dropped at build time, or simply absent from the corpus, contributes
    nothing) are scored, and their contributions are summed in **sorted term order** -- not
    query order -- so the floating-point total, and therefore the 6-dp rounding below, is the
    same in every implementation (the JavaScript port included) regardless of how the query
    was phrased.
    Docs whose total score is exactly `0.0` after rounding are excluded; `kind`, when given, is
    applied BEFORE the top-`k` cut, so a filtered query can return fewer than `k` hits even when
    `k` unfiltered hits exist. Results are ordered by `(-score, doc_index)`, so ties break
    deterministically by the document's position in `index.docs` -- i.e. by `(kind, url, sha)`.
    `score` is rounded to 6 decimal places, and that ROUNDED value is what both the 0-exclusion
    check and the sort use (not the raw float), so the ranking a caller observes is exactly the
    ranking `tests/fixtures/lexical/queries.json` records.

    Raises:
        ValueError: `k` is not an integer in `[1, 50]`. This is checked before anything else, so
            an out-of-range `k` raises even for an empty or tokenless `q`.

    NOT guaranteed: `kind` is not validated against any fixed set of known kinds here (an unknown
    `kind` simply matches nothing); this function does not know or check whether `q` exceeds any
    caller-side length bound (see `handoff/C1-DESIGN.md` §1 for the Worker's own `q` length cap).
    """
    if isinstance(k, bool) or not isinstance(k, int) or not (_MIN_K <= k <= _MAX_K):
        raise ValueError(f"k must be an integer between {_MIN_K} and {_MAX_K}, got {k!r}")

    terms = query_terms(q, max_terms=max_terms)
    if not terms:
        return []

    doc_count = len(index.docs)
    raw_scores: dict[int, float] = {}
    for term in sorted(terms):
        postings = index.terms.get(term)
        if not postings:
            continue
        df = len(postings)
        idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
        for doc_index, tf in postings:
            doc = index.docs[doc_index]
            length_ratio = (doc.length / index.avgdl) if index.avgdl else 0.0
            contribution = _bm25_term_score(idf, tf, index.k1, index.b, length_ratio)
            raw_scores[doc_index] = raw_scores.get(doc_index, 0.0) + contribution

    scored: list[tuple[int, float, LexicalDoc]] = []
    for doc_index, raw_score in raw_scores.items():
        score = round(raw_score, 6)
        if score == 0.0:
            continue
        doc = index.docs[doc_index]
        if kind is not None and doc.kind != kind:
            continue
        scored.append((doc_index, score, doc))

    scored.sort(key=lambda item: (-item[1], item[0]))

    return [
        SearchHit(
            sha=doc.sha,
            url=doc.url,
            title=doc.title,
            section=doc.section,
            kind=doc.kind,
            score=score,
            snippet=doc.abstract,
        )
        for _doc_index, score, doc in scored[:k]
    ]
