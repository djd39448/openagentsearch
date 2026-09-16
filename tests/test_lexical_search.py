"""Package C2a, spec Tests item 4: the reference `search()` ranking.

Replays `tests/fixtures/lexical/queries.json` against `tests/fixtures/lexical/fixture-index-v1.json`
-- the same golden pair a JavaScript port (package C2b) replays -- plus focused unit tests for
behaviour the fixture alone does not pin down unambiguously (tie-breaking, bounds checking).
"""

import json
from pathlib import Path

import pytest

from openagentsearch.lexical.index import (
    LexicalCounts,
    LexicalDoc,
    LexicalIndex,
    load_lexical_index,
)
from openagentsearch.lexical.search import search

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "lexical"


def _load_queries() -> list[dict]:
    return json.loads((FIXTURES / "queries.json").read_text(encoding="utf-8"))


QUERIES = _load_queries()
INDEX = load_lexical_index(FIXTURES / "fixture-index-v1.json")


def _doc(sha: str, kind: str = "k", length: int = 5) -> LexicalDoc:
    return LexicalDoc(
        sha=sha, url=f"https://x.test/{sha}", title=sha, section="", kind=kind,
        abstract="", length=length,
    )


def _index(docs: tuple[LexicalDoc, ...], terms: dict, avgdl: float = 5.0) -> LexicalIndex:
    return LexicalIndex(
        schema="openagentsearch.lexical-index/1",
        generated_at="2026-01-01T00:00:00Z",
        db_sha256="0" * 64,
        tokenizer="unicode-word-casefold-v1",
        k1=1.2,
        b=0.75,
        avgdl=avgdl,
        docs=docs,
        terms=terms,
        counts=LexicalCounts(
            docs=len(docs), terms=len(terms), postings=0, dropped_terms=0, missing_extracted=0
        ),
    )


# 4a. every queries.json case reproduces exactly --------------------------------------------


def test_queries_fixture_has_20_cases():
    assert len(QUERIES) == 20


@pytest.mark.parametrize(
    "case",
    QUERIES,
    ids=[f"{c['q']!r} k={c['k']} kind={c['kind']}" for c in QUERIES],
)
def test_golden_query_reproduces_exactly(case: dict):
    hits = search(INDEX, case["q"], case["k"], kind=case["kind"])
    assert [h.sha for h in hits] == [h["sha"] for h in case["hits"]]
    assert [h.score for h in hits] == [h["score"] for h in case["hits"]]


def test_scores_are_rounded_to_six_decimal_places():
    for case in QUERIES:
        for hit in case["hits"]:
            assert hit["score"] == round(hit["score"], 6)


# 4b. ties break by doc index ----------------------------------------------------------------


def test_ties_break_by_doc_index():
    doc_a = _doc("aaa" * 21 + "a", length=5)  # doc_index 0
    doc_b = _doc("bbb" * 21 + "b", length=5)  # doc_index 1
    terms = {"tie": ((0, 1), (1, 1))}
    index = _index((doc_a, doc_b), terms)
    hits = search(index, "tie", 5)
    assert [h.sha for h in hits] == [doc_a.sha, doc_b.sha]
    assert hits[0].score == hits[1].score


def test_golden_fixture_contains_a_real_tie():
    """Cross-check: at least one golden case in the committed fixture has two hits with an equal
    score, so `test_ties_break_by_doc_index` above is not testing a scenario the real fixture
    never exercises."""
    assert any(
        len(c["hits"]) >= 2 and c["hits"][0]["score"] == c["hits"][1]["score"] for c in QUERIES
    )


# 4c. kind filter -----------------------------------------------------------------------------


def test_kind_filter_excludes_other_kinds():
    doc_a = _doc("a" * 64, kind="room", length=5)
    doc_b = _doc("b" * 64, kind="site", length=5)
    terms = {"shared": ((0, 1), (1, 1))}
    index = _index((doc_a, doc_b), terms)
    hits = search(index, "shared", 5, kind="room")
    assert [h.sha for h in hits] == [doc_a.sha]


def test_kind_filter_is_applied_before_the_top_k_cut():
    """3 docs match, 2 of them 'room'; k=1 with kind='room' must return a 'room' doc, not
    whichever doc happens to score highest overall."""
    doc_a = _doc("a" * 64, kind="site", length=1)  # highest score: shortest doc
    doc_b = _doc("b" * 64, kind="room", length=50)
    doc_c = _doc("c" * 64, kind="room", length=50)
    terms = {"shared": ((0, 1), (1, 1), (2, 1))}
    index = _index((doc_a, doc_b, doc_c), terms, avgdl=30.0)
    unfiltered = search(index, "shared", 1)
    assert unfiltered[0].sha == doc_a.sha  # confirms doc_a really does score highest unfiltered

    filtered = search(index, "shared", 1, kind="room")
    assert filtered[0].kind == "room"


# 4d. k out of range raises --------------------------------------------------------------------


@pytest.mark.parametrize("bad_k", [0, -1, 51, 1000])
def test_k_out_of_range_raises(bad_k: int):
    with pytest.raises(ValueError):
        search(INDEX, "authentication", bad_k)


@pytest.mark.parametrize("good_k", [1, 50])
def test_k_at_the_boundary_is_accepted(good_k: int):
    search(INDEX, "authentication", good_k)  # must not raise


def test_non_integer_k_raises():
    with pytest.raises(ValueError):
        search(INDEX, "authentication", True)  # bool is not a valid k, even though isinstance(int)


def test_k_out_of_range_raises_even_for_an_empty_query():
    with pytest.raises(ValueError):
        search(INDEX, "", 0)


# 4e. empty / tokenless query -> [] ------------------------------------------------------------


def test_empty_query_returns_no_hits():
    assert search(INDEX, "", 5) == []


def test_tokenless_query_returns_no_hits():
    assert search(INDEX, "!!! ??? ...", 5) == []


# 4f. a query with 40 distinct terms considers only 32 ------------------------------------------


def test_query_with_40_distinct_terms_considers_only_32():
    doc_a = _doc("a" * 64, length=1)
    # 40 distinct single-use terms, term0..term39; only the first 32 (term0..term31) are ever
    # considered by query_terms(), so a term only doc_a happens to hold at index 35 must not match.
    terms = {f"term{i}": ((0, 1),) for i in range(32)}
    index = _index((doc_a,), terms, avgdl=1.0)
    q = " ".join(f"term{i}" for i in range(40))
    hits = search(index, q, 5)
    assert len(hits) == 1
    assert hits[0].sha == doc_a.sha  # matched via term0..term31, present in the index


def test_a_term_beyond_the_32nd_is_never_scored():
    doc_a = _doc("a" * 64, length=1)
    doc_b = _doc("b" * 64, length=1)
    terms = {f"term{i}": ((0, 1),) for i in range(32)}
    terms["term39"] = ((1, 1),)  # only reachable if max_terms did not actually cut at 32
    index = _index((doc_a, doc_b), terms, avgdl=1.0)
    q = " ".join(f"term{i}" for i in range(40))
    hits = search(index, q, 5)
    assert doc_b.sha not in [h.sha for h in hits]


# 4g. avgdl == 0 does not raise -----------------------------------------------------------------


def test_avgdl_zero_treats_length_ratio_as_zero():
    doc_a = _doc("a" * 64, length=0)
    terms = {"empty": ((0, 1),)}
    index = _index((doc_a,), terms, avgdl=0.0)
    hits = search(index, "empty", 5)  # must not raise ZeroDivisionError
    assert len(hits) == 1


# 4h. a doc with score exactly zero is excluded --------------------------------------------------


def test_absent_term_yields_no_hits_not_a_zero_score_hit():
    doc_a = _doc("a" * 64, length=5)
    index = _index((doc_a,), {}, avgdl=5.0)
    assert search(index, "nonexistent", 5) == []


# 4i. snippet is the doc's abstract, verbatim ----------------------------------------------------


def test_snippet_is_the_doc_abstract():
    doc_a = LexicalDoc(
        sha="a" * 64, url="https://x.test/a", title="T", section="", kind="k",
        abstract="the abstract text", length=5,
    )
    index = _index((doc_a,), {"word": ((0, 1),)}, avgdl=5.0)
    hits = search(index, "word", 5)
    assert hits[0].snippet == "the abstract text"
