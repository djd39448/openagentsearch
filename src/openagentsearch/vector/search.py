"""Deterministic cosine similarity search over a VectorStore (standard library only)."""

import math
from typing import List, Tuple

from .store import VectorStore


def cosine_search(store: VectorStore, query_vector: List[float], k: int) -> List[Tuple[str, float]]:
    """Return the top-k (chunk_id, score) pairs by cosine similarity to query_vector.

    Results are sorted by descending score, then ascending chunk_id, so ties are deterministic.
    If k exceeds the number of stored records, all records are returned; an empty store yields [].

    Raises:
        ValueError: if k <= 0; if the query vector is empty, all zeros, or contains boolean or
            non-numeric elements; or if any stored vector does not have the query's dimension.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    if not query_vector:
        raise ValueError("query vector cannot be empty")
    for i, value in enumerate(query_vector):
        if isinstance(value, bool):
            raise ValueError(f"query vector element at index {i} is a boolean, must be numeric")
        if not isinstance(value, (int, float)):
            raise ValueError(f"query vector element at index {i} is not numeric: {value!r}")
    if all(x == 0 for x in query_vector):
        raise ValueError("query vector cannot be all zeros")

    query = [float(x) for x in query_vector]
    dimension = len(query)
    query_magnitude = math.sqrt(sum(x * x for x in query))

    records = store.load_all()

    # Validate every stored vector before producing any result.
    for record in records:
        stored = record["vector"]
        if len(stored) != dimension:
            raise ValueError(
                f"stored vector for {record['chunk_id']} has dimension {len(stored)}, "
                f"which does not match query vector dimension {dimension}"
            )

    results: List[Tuple[str, float]] = []
    for record in records:
        stored = record["vector"]
        stored_magnitude = math.sqrt(sum(x * x for x in stored))
        if stored_magnitude == 0.0:
            # A stored zero vector is valid data; it simply cannot be similar to anything.
            score = 0.0
        else:
            dot_product = sum(a * b for a, b in zip(query, stored))
            score = dot_product / (query_magnitude * stored_magnitude)
        score = max(-1.0, min(1.0, float(score)))
        results.append((str(record["chunk_id"]), score))

    results.sort(key=lambda item: (-item[1], item[0]))
    return results[:k]
