"""Tests for cosine_search over a VectorStore (hand-made vectors, no embeddings, no network)."""

import math
import tempfile
from pathlib import Path

from openagentsearch.vector.search import cosine_search
from openagentsearch.vector.store import VectorStore


def _store(tmpdir: str, dimension: int) -> VectorStore:
    return VectorStore(Path(tmpdir) / "v.sqlite3", dimension)


def test_nearest_ranks_first_with_expected_score_order():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 3)
        store.add("x_axis", "doc1", [1.0, 0.0, 0.0], "x")
        store.add("y_axis", "doc1", [0.0, 1.0, 0.0], "y")
        store.add("diagonal", "doc2", [1.0, 1.0, 0.0], "xy")
        store.add("opposite", "doc2", [-1.0, 0.0, 0.0], "-x")

        results = cosine_search(store, [1.0, 0.0, 0.0], k=4)

        ids = [chunk_id for chunk_id, _ in results]
        scores = [score for _, score in results]
        assert ids == ["x_axis", "diagonal", "y_axis", "opposite"]
        assert scores[0] == 1.0
        assert math.isclose(scores[1], 1 / math.sqrt(2))
        assert scores[2] == 0.0
        assert scores[3] == -1.0
        assert all(isinstance(score, float) for score in scores)
        store.close()


def test_equal_scores_tie_break_by_chunk_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 2)
        store.add("zed", "doc1", [2.0, 0.0], "z")
        store.add("alpha", "doc2", [1.0, 0.0], "a")
        store.add("mid", "doc3", [3.0, 0.0], "m")

        results = cosine_search(store, [1.0, 0.0], k=3)

        assert [chunk_id for chunk_id, _ in results] == ["alpha", "mid", "zed"]
        assert all(score == 1.0 for _, score in results)
        store.close()


def test_k_larger_than_record_count_returns_all_and_empty_store_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        empty = _store(tmpdir, 2)
        assert cosine_search(empty, [1.0, 0.0], k=5) == []
        empty.close()

        store = VectorStore(Path(tmpdir) / "two.sqlite3", 2)
        store.add("a", "doc1", [1.0, 0.0], "a")
        store.add("b", "doc2", [0.0, 1.0], "b")
        results = cosine_search(store, [1.0, 0.0], k=10)
        assert [chunk_id for chunk_id, _ in results] == ["a", "b"]
        store.close()


def test_k_zero_or_negative_raises_value_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 2)
        store.add("a", "doc1", [1.0, 0.0], "a")
        for bad_k in (0, -1):
            try:
                cosine_search(store, [1.0, 0.0], k=bad_k)
                assert False, f"k={bad_k} should have raised ValueError"
            except ValueError as exc:
                assert "k must be positive" in str(exc)
        store.close()


def test_zero_query_raises_but_stored_zero_vector_scores_zero():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 2)
        store.add("real", "doc1", [1.0, 0.0], "r")
        store.add("zero", "doc2", [0.0, 0.0], "z")

        try:
            cosine_search(store, [0.0, 0.0], k=1)
            assert False, "an all-zero query should have raised ValueError"
        except ValueError as exc:
            assert "all zeros" in str(exc)

        results = cosine_search(store, [1.0, 0.0], k=2)
        assert results == [("real", 1.0), ("zero", 0.0)]
        store.close()


def test_dimension_mismatch_raises_value_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 2)
        store.add("a", "doc1", [1.0, 0.0], "a")
        try:
            cosine_search(store, [1.0, 0.0, 0.0], k=1)
            assert False, "a 3-d query against 2-d records should have raised ValueError"
        except ValueError as exc:
            assert "does not match query vector dimension" in str(exc)
        store.close()


def test_boolean_and_non_numeric_query_elements_raise_value_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _store(tmpdir, 2)
        store.add("a", "doc1", [1.0, 0.0], "a")
        for bad_query, fragment in (([1.0, True], "is a boolean"), ([1.0, "x"], "is not numeric"), ([None, 1.0], "is not numeric"), ([], "cannot be empty")):
            try:
                cosine_search(store, bad_query, k=1)
                assert False, f"{bad_query!r} should have raised ValueError"
            except ValueError as exc:
                assert fragment in str(exc)
        store.close()
