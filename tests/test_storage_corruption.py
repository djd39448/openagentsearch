"""Post-roadmap #4: corrupted persisted rows and non-finite vectors fail clearly, never incidentally."""

import json
import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from openagentsearch.vector.search import cosine_search
from openagentsearch.vector.store import StoreCorruptionError, VectorStore


def _seeded(path: Path) -> VectorStore:
    store = VectorStore(path, 2)
    store.add("a", "d" * 64, [1.0, 0.0], "alpha")
    return store


def _corrupt(path: Path, chunk_id: str, *, vector_json: str | None = None, dimension: int | None = None) -> None:
    """Simulate disk corruption through a separate sqlite3 connection while the store is closed."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            if vector_json is not None:
                conn.execute("UPDATE vectors SET vector_json = ? WHERE chunk_id = ?", (vector_json, chunk_id))
            if dimension is not None:
                conn.execute("UPDATE vectors SET dimension = ? WHERE chunk_id = ?", (dimension, chunk_id))
    finally:
        conn.close()


def test_vector_store_rejects_malformed_vector_json():
    assert issubclass(StoreCorruptionError, ValueError)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "v.sqlite3"
        store = _seeded(path)
        store.close()
        _corrupt(path, "a", vector_json="{not json")
        store = VectorStore(path, 2)
        try:
            with pytest.raises(StoreCorruptionError) as info:
                store.get("a")
            assert isinstance(info.value.__cause__, json.JSONDecodeError)
            assert "a" in str(info.value)
            with pytest.raises(StoreCorruptionError):
                store.load_all()
            assert store.count() == 1
        finally:
            store.close()


def test_vector_store_rejects_corrupt_vector_shapes():
    cases = {
        "object": json.dumps({"a": 1.0}),
        "wrong_length": json.dumps([1.0]),
        "string_element": json.dumps(["1.0", 0.0]),
        "bool_element": json.dumps([True, 0.0]),
        "null_element": json.dumps([None, 0.0]),
        "nan": json.dumps([float("nan"), 0.0]),
        "positive_infinity": json.dumps([float("inf"), 0.0]),
        "negative_infinity": json.dumps([float("-inf"), 0.0]),
    }
    assert cases["nan"] == "[NaN, 0.0]"  # json.dumps emits the non-standard literal; json.loads reads it back
    with tempfile.TemporaryDirectory() as tmpdir:
        for name, vector_json in cases.items():
            path = Path(tmpdir) / f"{name}.sqlite3"
            _seeded(path).close()
            _corrupt(path, "a", vector_json=vector_json)
            store = VectorStore(path, 2)
            try:
                with pytest.raises(StoreCorruptionError):
                    store.get("a")
                with pytest.raises(StoreCorruptionError):
                    store.load_all()
                assert store.count() == 1
            finally:
                store.close()
        for name, dimension in (("zero_dimension", 0), ("other_dimension", 3)):
            path = Path(tmpdir) / f"{name}.sqlite3"
            _seeded(path).close()
            _corrupt(path, "a", dimension=dimension)
            store = VectorStore(path, 2)
            try:
                with pytest.raises(StoreCorruptionError):
                    store.get("a")
                with pytest.raises(StoreCorruptionError):
                    store.load_all()
            finally:
                store.close()


def test_cosine_search_rejects_non_finite_query_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = _seeded(Path(tmpdir) / "v.sqlite3")
        store.add("b", "e" * 64, [0.0, 1.0], "beta")
        try:
            for bad in ([float("nan"), 0.0], [float("inf"), 0.0], [0.0, float("-inf")]):
                with pytest.raises(ValueError):
                    cosine_search(store, bad, 2)
            try:
                extreme = cosine_search(store, [1e308, 1e308], 2)
            except ValueError:
                extreme = []
            assert all(math.isfinite(score) for _, score in extreme)
            assert cosine_search(store, [1.0, 0.0], 2) == [("a", 1.0), ("b", 0.0)]
        finally:
            store.close()


class _StoreDouble:
    def __init__(self, vector: list[float]) -> None:
        self.record = {"chunk_id": "x", "doc_sha256": "f" * 64, "vector": vector, "text": "t"}

    def load_all(self) -> list[dict]:
        return [dict(self.record)]

    def get(self, chunk_id: str) -> dict | None:
        return dict(self.record) if chunk_id == "x" else None


def test_cosine_search_defensively_rejects_non_finite_stored_values():
    for stored in ([float("nan"), 0.0], [float("inf"), 0.0], [0.0, float("-inf")]):
        with pytest.raises(ValueError):
            cosine_search(_StoreDouble(stored), [1.0, 0.0], 1)
    assert cosine_search(_StoreDouble([0.0, 0.0]), [1.0, 0.0], 1) == [("x", 0.0)]
