"""Package D1: the fail-closed `SessionOffer` parsing seam (`openagentsearch.flop.offer`) --
every input answers `OFFER_SHAPE_UNPUBLISHED` (the wire shape is not public; see the module
docstring), never "accepted," never "rejected as malformed.\""""

import hashlib
import random
from glob import glob
from pathlib import Path

import pytest

from openagentsearch.flop.offer import (
    OFFER_SHAPE_UNPUBLISHED,
    OfferParseResult,
    OfferShapeStatus,
    offer_shape_status,
    parse_session_offer,
)

REPO = Path(__file__).resolve().parents[1]


def _wire_corpus_prefix(n: int) -> bytes:
    matches = glob(str(REPO / "tests" / "fixtures" / "**" / "*wire*"), recursive=True)
    files = sorted(Path(m) for m in matches if Path(m).is_file())
    assert files, "no wire corpus fixture file found under tests/fixtures"
    return files[0].read_bytes()[:n]


# 1. Done-when: every kind of input answers OFFER_SHAPE_UNPUBLISHED, with an honest length+hash --


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{}",
        (
            b'{"miner": "m1", "chain_genesis": "g1", "model_hash": "h1", "precision": "fp16", '
            b'"enclave_key": "ek1", "minimum_escrow": 100, "sla_bounds": [1, 2], '
            b'"advisory_capacity_hint": 5, "expiry": 1700000000, "nonce": "n1", '
            b'"signature": "sig1", "forward_terms": null}'
        ),
        random.Random(0).randbytes(4096),
        _wire_corpus_prefix(512),
    ],
)
def test_every_input_answers_offer_shape_unpublished(data: bytes) -> None:
    result = parse_session_offer(data)

    assert result.status == "OFFER_SHAPE_UNPUBLISHED"
    assert result.shape.published is False
    assert result.input_bytes == len(data)
    assert result.input_sha256 == hashlib.sha256(data).hexdigest()


# 2. Determinism -----------------------------------------------------------------------------


def test_parse_is_deterministic_for_equal_bytes() -> None:
    data = b"some plausible offer bytes"

    first = parse_session_offer(data)
    second = parse_session_offer(bytes(data))

    assert first == second


# 3. Bounds -----------------------------------------------------------------------------------


def test_parse_rejects_non_bytes_with_type_error() -> None:
    with pytest.raises(TypeError):
        parse_session_offer("x")  # type: ignore[arg-type]


def test_parse_accepts_bytearray() -> None:
    result = parse_session_offer(bytearray(b"abc"))
    assert result.input_bytes == 3
    assert result.input_sha256 == hashlib.sha256(b"abc").hexdigest()


def test_parse_accepts_data_exactly_at_max_bytes() -> None:
    data = b"x" * 10
    result = parse_session_offer(data, max_bytes=10)
    assert result.input_bytes == 10


def test_parse_rejects_data_one_byte_over_max_bytes() -> None:
    data = b"x" * 11
    with pytest.raises(ValueError):
        parse_session_offer(data, max_bytes=10)


def test_parse_rejects_zero_max_bytes() -> None:
    with pytest.raises(ValueError):
        parse_session_offer(b"", max_bytes=0)


def test_parse_rejects_negative_max_bytes() -> None:
    with pytest.raises(ValueError):
        parse_session_offer(b"x", max_bytes=-1)


def test_parse_rejects_bool_max_bytes() -> None:
    with pytest.raises(ValueError):
        parse_session_offer(b"x", max_bytes=True)


# 4. offer_shape_status() ------------------------------------------------------------------------


def test_offer_shape_status_is_the_same_object_every_call() -> None:
    assert offer_shape_status() is offer_shape_status()


def test_offer_shape_status_is_unpublished_with_nonempty_watch() -> None:
    status = offer_shape_status()

    assert isinstance(status, OfferShapeStatus)
    assert status.published is False
    assert len(status.watch) > 0


def test_offer_shape_status_binds_has_twelve_unique_snake_case_names() -> None:
    status = offer_shape_status()

    assert len(status.binds) == 12
    assert len(set(status.binds)) == 12
    assert "model_hash" in status.binds
    assert "precision" in status.binds
    for name in status.binds:
        assert name == name.lower()
        assert " " not in name
        assert name.replace("_", "").isalnum()


# 5. Doc guard: the forbidden appendix citation never appears -----------------------------------


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/openagentsearch/flop/offer.py",
        "docs/flop-wire.md",
        "README.md",
        "CHANGELOG.md",
    ],
)
def test_forbidden_citation_does_not_appear(relative_path: str) -> None:
    text = (REPO / relative_path).read_text(encoding="utf-8")
    assert "E.54" not in text


# Result type sanity (spec item 6, "all previously passing tests stay green", is a whole-suite
# property checked by the orchestrator's rerun, not by any single test here) ---------------------


def test_offer_parse_result_type_and_status_constant() -> None:
    result = parse_session_offer(b"abc")

    assert isinstance(result, OfferParseResult)
    assert result.status == OFFER_SHAPE_UNPUBLISHED
    assert isinstance(result.reason, str) and result.reason
