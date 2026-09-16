"""Package C2a, spec Tests item 1: the `unicode-word-casefold-v1` tokenizer.

Everything here is offline and pure -- no filesystem writes, no subprocess, nothing that could
hang -- except the one fixture file read, which is a small, repo-local JSON file.
"""

import json
from pathlib import Path

import pytest

from openagentsearch.lexical.tokenize import query_terms, tokenize

REPO = Path(__file__).resolve().parents[1]
VECTORS_PATH = REPO / "tests" / "fixtures" / "lexical" / "tokenizer-vectors.json"


def _load_vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


VECTORS = _load_vectors()


# 1a. every tokenizer vector ----------------------------------------------------------------


def test_tokenizer_vectors_file_has_at_least_30_cases():
    assert len(VECTORS) >= 30, len(VECTORS)


@pytest.mark.parametrize("vector", VECTORS, ids=[repr(v["input"])[:40] for v in VECTORS])
def test_tokenizer_vector(vector: dict):
    assert tokenize(vector["input"]) == vector["tokens"]


def test_tokenizer_vectors_cover_the_documented_edge_cases():
    """Sanity-check the fixture itself demonstrates the specific rules the spec calls out, so a
    change that accidentally deletes a case (rather than merely re-orders the list) is caught
    here even without inspecting the JSON file by eye."""
    by_input = {v["input"]: v["tokens"] for v in VECTORS}

    # underscore identifier: whole run + each `_`-separated part that passes the length rule.
    ident = "min_force_open_escrow_for_failed_ack"
    assert by_input[ident][0] == ident
    for part in ("min", "force", "open", "escrow", "for", "failed", "ack"):
        assert part in by_input[ident]

    # a did:key -- the key material run is over 40 chars and is dropped entirely.
    did = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"
    assert by_input[did] == ["did", "key"]

    # hyphenation splits into separate tokens.
    assert by_input["off-chain"] == ["off", "chain"]

    # fullwidth digits/letters NFKC-normalize before tokenizing.
    assert by_input["ＡＢＣ０１２"] == ["abc012"]

    # casefold, not merely lowercase: German eszett folds to "ss".
    assert by_input["Straße"] == ["strasse"]

    # a CJK run with no internal word-boundary characters stays one token.
    assert by_input["中文字符"] == ["中文字符"]

    # emoji-only text tokenizes to nothing.
    assert by_input["\U0001F389\U0001F525"] == []

    # a single-character token is dropped (min length 2).
    assert by_input["a bb"] == ["bb"]

    # a 41-char token is dropped, a 40-char token is kept, in the same input.
    long_kept = "a" * 40
    long_dropped = "a" * 41
    assert by_input[f"{long_kept} {long_dropped}"] == [long_kept]

    # leading/trailing underscores: the whole 3-char run is kept, its 1-char part is not.
    assert by_input["_x_"] == ["_x_"]

    # empty and whitespace-only input tokenize to nothing.
    assert by_input[""] == []
    assert by_input["   "] == []


# 1b. the oversize input raises -----------------------------------------------------------------


def test_tokenize_rejects_input_over_one_million_characters():
    with pytest.raises(ValueError):
        tokenize("a" * 1_000_001)


def test_tokenize_accepts_input_at_exactly_one_million_characters():
    # The boundary itself must NOT raise -- only strictly over it does. A 1,000,000-char run of
    # the same word character is one run, far over the 40-char max token length, so it yields no
    # tokens at all (dropped, not truncated) -- this asserts "did not raise", not "found a token".
    assert tokenize("a" * 1_000_000) == []


def test_tokenize_duplicates_and_order_are_preserved():
    assert tokenize("cat dog cat") == ["cat", "dog", "cat"]


# 1c. query_terms dedupes and cuts at max_terms --------------------------------------------------


def test_query_terms_dedupes_preserving_first_seen_order():
    assert query_terms("cat dog cat bird dog") == ["cat", "dog", "bird"]


def test_query_terms_cuts_at_max_terms_default_32():
    text = " ".join(f"term{i}" for i in range(40))
    terms = query_terms(text)
    assert len(terms) == 32
    assert terms == [f"term{i}" for i in range(32)]


def test_query_terms_respects_a_custom_max_terms():
    text = " ".join(f"term{i}" for i in range(10))
    assert query_terms(text, max_terms=3) == ["term0", "term1", "term2"]


def test_query_terms_of_empty_text_is_empty():
    assert query_terms("") == []


def test_query_terms_propagates_the_tokenize_length_bound():
    with pytest.raises(ValueError):
        query_terms("a" * 1_000_001)
