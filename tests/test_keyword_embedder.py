"""Unit tests for the deterministic hashed-keyword embedder (no network, no randomness)."""

import math

import pytest

from openagentsearch.embed.keyword import KeywordEmbedder


def test_deterministic_across_instances_and_calls():
    a = KeywordEmbedder(64)
    b = KeywordEmbedder(64)
    text = "The quick brown fox jumps over the lazy dog, 42 times!"
    v1 = a.embed(text)
    v2 = b.embed(text)
    v3 = a.embed(text)
    assert v1 == v2 == v3


def test_dimension_is_respected():
    for dimension in (8, 16, 256, 1000):
        embedder = KeywordEmbedder(dimension)
        assert len(embedder.embed("some words here")) == dimension
        assert len(embedder.embed("")) == dimension


def test_unit_norm_for_non_empty_text():
    embedder = KeywordEmbedder(32)
    for text in ("hello world", "a", "42 is the answer", "repeat repeat repeat words words"):
        vector = embedder.embed(text)
        norm = math.sqrt(sum(x * x for x in vector))
        assert math.isclose(norm, 1.0, rel_tol=1e-9, abs_tol=1e-12), (text, norm)


def test_zero_vector_for_tokenless_text():
    embedder = KeywordEmbedder(32)
    for text in ("", "   ", "!!!???...", "---***", "\t\n  "):
        vector = embedder.embed(text)
        assert vector == [0.0] * 32, text


def test_case_and_punctuation_insensitive_tokenization():
    embedder = KeywordEmbedder(128)
    assert embedder.embed("Hello, World!") == embedder.embed("hello world")
    assert embedder.embed("HELLO-WORLD") == embedder.embed("hello world")
    assert embedder.embed("Hello   World") == embedder.embed("hello world")
    # Order of tokens does not matter for a bag-of-tokens count.
    assert embedder.embed("hello world") == embedder.embed("world hello")
    # Punctuation glued to a token does not merge it with an adjacent token.
    assert embedder.embed("hello, world.") == embedder.embed("hello world")


def test_repeated_tokens_increase_bucket_count_but_stay_unit_norm():
    embedder = KeywordEmbedder(64)
    once = embedder.embed("apple")
    twice = embedder.embed("apple apple")
    # A single repeated token still only lights up one bucket, so it normalizes to the same vector.
    assert once == twice
    norm = math.sqrt(sum(x * x for x in twice))
    assert math.isclose(norm, 1.0, rel_tol=1e-9)


def test_distinct_single_tokens_are_orthonormal_basis_vectors_when_no_collision():
    embedder = KeywordEmbedder(4096)  # large dimension keeps collisions astronomically unlikely
    v_cat = embedder.embed("cat")
    v_dog = embedder.embed("dog")
    assert v_cat != v_dog
    dot = sum(a * b for a, b in zip(v_cat, v_dog))
    assert math.isclose(dot, 0.0, abs_tol=1e-9)


@pytest.mark.parametrize("bad_dimension", [0, 1, 7, -8, -1])
def test_dimension_below_minimum_raises_value_error(bad_dimension):
    with pytest.raises(ValueError):
        KeywordEmbedder(bad_dimension)


@pytest.mark.parametrize("bad_dimension", [8.0, "256", None, [256], True, False])
def test_non_int_or_bool_dimension_raises_value_error(bad_dimension):
    with pytest.raises(ValueError):
        KeywordEmbedder(bad_dimension)


def test_default_dimension_is_256():
    embedder = KeywordEmbedder()
    assert embedder.dimension == 256
    assert len(embedder.embed("anything")) == 256
