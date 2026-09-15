"""A deterministic, dependency-free "embedder" for tests and demos (standard library only).

`KeywordEmbedder` is a hashed bag-of-tokens vectorizer, not a semantic model. What it does NOT
guarantee:

- No semantics: it has no notion of meaning, synonymy, word order, or grammar - only which
  lowercase alphanumeric tokens appear and how often.
- Hash collisions: distinct tokens can land in the same bucket (`hashlib.sha256(token) %
  dimension`), especially at small `dimension`; two unrelated texts can therefore score as
  similar.
- Not comparable across embedders: a vector from this class is meaningless compared (by cosine
  or any other measure) against a vector from `OllamaEmbedClient` or any other embedder - they
  do not share a coordinate system.
- Determinism relies on `hashlib.sha256`, never Python's salted `hash()` builtin, so the same
  text yields the same vector in this process, in a fresh process, and on another machine.

Intended use: fixtures, offline demos, and tests that need a stable, network-free `embed(text) ->
list[float]` without pulling in a real model.
"""

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class KeywordEmbedder:
    """Hashes lowercase alphanumeric tokens into `dimension` buckets, counts them, and
    L2-normalizes the result. See the module docstring for what this is NOT."""

    def __init__(self, dimension: int = 256) -> None:
        """Raises ValueError if `dimension` is not a plain `int` (booleans rejected explicitly,
        since `bool` is a subclass of `int`) or is less than 8."""
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            raise ValueError(f"dimension must be an int, got {dimension!r}")
        if dimension < 8:
            raise ValueError(f"dimension must be >= 8, got {dimension}")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalized vector of length `dimension` for `text`.

        Tokenization is `re.findall(r"[a-z0-9]+", text.lower())`, so tokenization is case- and
        punctuation-insensitive (case is folded, and any non-alphanumeric character is a
        separator, never part of a token). Each token is hashed with
        `hashlib.sha256(token.encode("utf-8"))` and mapped to bucket
        `int(hexdigest, 16) % dimension`; buckets are then counted.

        A text with no tokens (empty string, or text with no ASCII letters/digits at all) returns
        the all-zero vector unchanged - it is never divided by a zero norm, and it is NOT unit
        length. Callers that feed this into a cosine-similarity search must handle an all-zero
        query vector themselves (for example, `openagentsearch.vector.search.cosine_search`
        rejects an all-zero query vector by raising `ValueError`).
        """
        if not isinstance(text, str):
            raise ValueError(f"text must be a string, got {text!r}")
        counts = [0.0] * self.dimension
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            bucket = int(digest, 16) % self.dimension
            counts[bucket] += 1.0
        norm = math.sqrt(sum(c * c for c in counts))
        if norm == 0.0:
            return counts
        return [c / norm for c in counts]
