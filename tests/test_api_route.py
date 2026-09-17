"""Package D2, spec Tests item 3: `GET /route` -- every validation branch, the empty-
candidates/null-ranking invariant, DID extraction and ledger join (known/unknown/no-ledger),
byte-parity of `offer_shape` with `openagentsearch.flop.offer.offer_shape_status()`, byte-parity
of the `dids[].ledger` body with `/did/{did}` itself, a real-server test through `create_server`,
and (package D2's own generated artifact) that `worker/src/offer-shape.json` matches a fresh
generation of `scripts/make_offer_shape_json.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from openagentsearch.api.did import make_did_prefix_route
from openagentsearch.api.route import (
    ObservationHit,
    RouteError,
    RouteParams,
    extract_dids,
    make_route_route,
    parse_route_params,
)
from openagentsearch.api.server import create_server
from openagentsearch.flop.offer import offer_shape_status
from openagentsearch.reputation.compact import write_compact_ledger, load_compact_ledger
from openagentsearch.reputation.ledger import build_ledger
from reputation_fixture_support import DID_A, DID_B, NOW, install_fixture

REPO = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT = 60
UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"
HTTP_TIMEOUT = 5.0


def _call(route: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, object], dict[str, str]]:
    """Calls a `JSONRoute` and normalizes its `RouteResult` to `(status, body, extra_headers)` --
    `route()`'s own 200 path always returns a 3-tuple (an empty `extra_headers` dict when there is
    nothing to report), its 400 path a plain 2-tuple -- the same normalization
    `openagentsearch.api.server._split_result` performs for every registered route."""
    result = route(query)
    if len(result) == 3:
        return result[0], result[1], result[2]
    return result[0], result[1], {}


# --- parse_route_params: validation branches ------------------------------------------------


def test_missing_model_hash() -> None:
    result = parse_route_params({})
    assert result == RouteError("missing_model_hash", "model_hash")


@pytest.mark.parametrize(
    "bad",
    ["", "a" * 129, "has a space", "has$dollar", "has#hash", "emoji\U0001f600"],
)
def test_invalid_model_hash(bad: str) -> None:
    result = parse_route_params({"model_hash": [bad]})
    assert result == RouteError("invalid_model_hash", "model_hash")


def test_model_hash_min_and_max_length_accepted() -> None:
    for value in ("a", "a" * 128):
        result = parse_route_params({"model_hash": [value]})
        assert isinstance(result, RouteParams)
        assert result.model_hash == value


def test_model_hash_full_alphabet_accepted() -> None:
    value = "abcXYZ019:_./-"
    result = parse_route_params({"model_hash": [value]})
    assert isinstance(result, RouteParams)
    assert result.model_hash == value


def test_precision_optional_and_defaults_to_none() -> None:
    result = parse_route_params({"model_hash": ["m1"]})
    assert isinstance(result, RouteParams)
    assert result.precision is None


@pytest.mark.parametrize("bad", ["", "a" * 33, "bad space"])
def test_invalid_precision(bad: str) -> None:
    result = parse_route_params({"model_hash": ["m1"], "precision": [bad]})
    assert result == RouteError("invalid_precision", "precision")


def test_precision_not_checked_against_any_vocabulary() -> None:
    # Not "fp16"/"fp32"/etc -- any token in the alphabet is accepted, the vocabulary is unpublished.
    result = parse_route_params({"model_hash": ["m1"], "precision": ["not-a-real-precision-kind"]})
    assert isinstance(result, RouteParams)
    assert result.precision == "not-a-real-precision-kind"


@pytest.mark.parametrize("bad", ["", "abc", "-1", "1.5", "0", "600001", "999999999999"])
def test_invalid_max_latency_ms(bad: str) -> None:
    result = parse_route_params({"model_hash": ["m1"], "max_latency_ms": [bad]})
    assert result == RouteError("invalid_max_latency_ms", "max_latency_ms")


def test_max_latency_ms_bounds_accepted() -> None:
    for value in ("1", "600000"):
        result = parse_route_params({"model_hash": ["m1"], "max_latency_ms": [value]})
        assert isinstance(result, RouteParams)
        assert result.max_latency_ms == int(value)


def test_max_latency_ms_optional_and_defaults_to_none() -> None:
    result = parse_route_params({"model_hash": ["m1"]})
    assert isinstance(result, RouteParams)
    assert result.max_latency_ms is None


@pytest.mark.parametrize("bad", ["", "abc", "-1", "1.5", "0", "51", "０"])
def test_invalid_k(bad: str) -> None:
    result = parse_route_params({"model_hash": ["m1"], "k": [bad]})
    assert result == RouteError("invalid_k", "k")


def test_k_bounds_accepted_and_default() -> None:
    result = parse_route_params({"model_hash": ["m1"]})
    assert isinstance(result, RouteParams)
    assert result.k == 10
    for value in ("1", "50"):
        result = parse_route_params({"model_hash": ["m1"], "k": [value]})
        assert isinstance(result, RouteParams)
        assert result.k == int(value)


def test_repeated_parameters_first_value_wins() -> None:
    result = parse_route_params({"model_hash": ["first", "second"]})
    assert isinstance(result, RouteParams)
    assert result.model_hash == "first"


def test_validation_order_model_hash_before_everything_else() -> None:
    # A request with several problems at once reports the FIRST one, per the documented order.
    result = parse_route_params(
        {"model_hash": [""], "precision": ["bad space"], "k": ["nope"]}
    )
    assert result == RouteError("invalid_model_hash", "model_hash")


def test_validation_order_precision_before_max_latency_and_k() -> None:
    result = parse_route_params(
        {
            "model_hash": ["m1"],
            "precision": ["bad space"],
            "max_latency_ms": ["nope"],
            "k": ["nope"],
        }
    )
    assert result == RouteError("invalid_precision", "precision")


def test_validation_order_max_latency_before_k() -> None:
    result = parse_route_params({"model_hash": ["m1"], "max_latency_ms": ["nope"], "k": ["nope"]})
    assert result == RouteError("invalid_max_latency_ms", "max_latency_ms")


# --- extract_dids ------------------------------------------------------------------------------


def test_extract_dids_finds_and_orders_by_first_appearance() -> None:
    text = f"first {DID_B} then {DID_A} then {DID_B} again"
    assert extract_dids(text) == (DID_B, DID_A)


def test_extract_dids_dedupes() -> None:
    text = f"{DID_A} {DID_A} {DID_A}"
    assert extract_dids(text) == (DID_A,)


# Base58 (`did:key:z...`'s own alphabet): digits and letters MINUS 0, O, I, l -- used to build
# synthetic-but-valid did:key tokens without accidentally embedding an excluded character (a
# literal "0", for example, would silently truncate the intended token mid-match).
_SAFE_BASE58_CHARS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"


def test_extract_dids_caps_at_limit_five_by_default() -> None:
    dids = [f"did:key:z6Mk{_SAFE_BASE58_CHARS[i]}suffix" for i in range(8)]
    text = " ".join(dids)
    found = extract_dids(text)
    assert len(found) == 5
    assert found == tuple(dids[:5])


def test_extract_dids_custom_limit() -> None:
    dids = [f"did:key:z6Mk{_SAFE_BASE58_CHARS[i]}suffix" for i in range(4)]
    text = " ".join(dids)
    assert extract_dids(text, limit=2) == tuple(dids[:2])


def test_extract_dids_empty_when_none_present() -> None:
    assert extract_dids("nothing to see here") == ()


# --- make_route_route: the invariant, DID join, offer_shape parity ----------------------------


def _hit(url: str, text: str, kind: str | None = None, score: float = 1.0) -> ObservationHit:
    return ObservationHit(url=url, kind=kind, score=score, text=text)


def test_candidates_always_empty_and_ranking_always_null() -> None:
    for hits in ([], [_hit("u", "no dids here")], [_hit("u", DID_A), _hit("u2", DID_B)]):

        def search(_q: str, _k: int, _hits: list[ObservationHit] = hits) -> list[ObservationHit]:
            return _hits

        route = make_route_route(search, None, "2026-09-17T00:00:00Z", None)
        status, body, _headers = _call(route, {"model_hash": ["m1"]})
        assert status == 200
        assert body["candidates"] == []
        assert body["ranking"] is None
        assert isinstance(body["candidates_reason"], str) and body["candidates_reason"]
        assert isinstance(body["ranking_reason"], str) and body["ranking_reason"]


def test_offer_shape_matches_offer_shape_status_field_for_field() -> None:
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return []

    route = make_route_route(search, None, "2026-09-17T00:00:00Z", None)
    status, body, _headers = _call(route, {"model_hash": ["m1"]})
    assert status == 200
    shape = offer_shape_status()
    assert body["offer_shape"] == {
        "published": shape.published,
        "source": shape.source,
        "watch": list(shape.watch),
        "binds": list(shape.binds),
    }
    assert body["advisory"] is True


def test_observations_query_uses_model_hash_alone_when_no_precision() -> None:
    seen: list[tuple[str, int]] = []

    def search(q: str, k: int) -> list[ObservationHit]:
        seen.append((q, k))
        return []

    route = make_route_route(search, None, "gen", None)
    _call(route, {"model_hash": ["m1"]})
    assert seen == [("m1", 10)]


def test_observations_query_joins_model_hash_and_precision_with_a_space() -> None:
    seen: list[tuple[str, int]] = []

    def search(q: str, k: int) -> list[ObservationHit]:
        seen.append((q, k))
        return []

    route = make_route_route(search, None, "gen", None)
    status, body, _headers = _call(route, {"model_hash": ["m1"], "precision": ["fp16"], "k": ["3"]})
    assert seen == [("m1 fp16", 3)]
    assert body["observations_query"] == "m1 fp16"


def test_query_object_echoes_every_field_including_max_latency_ms_unused() -> None:
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return []

    route = make_route_route(search, None, "gen", None)
    status, body, _headers = _call(
        route, {"model_hash": ["m1"], "precision": ["fp16"], "max_latency_ms": ["1500"], "k": ["7"]}
    )
    assert body["query"] == {
        "model_hash": "m1",
        "precision": "fp16",
        "max_latency_ms": 1500,
        "k": 7,
    }


def test_validation_error_body_names_the_field() -> None:
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return []

    route = make_route_route(search, None, "gen", None)
    status, body, _headers = _call(route, {})
    assert status == 400
    assert body == {"error": "missing_model_hash", "field": "model_hash"}


def test_no_ledger_loaded_answers_ledger_not_built_for_every_did_mention() -> None:
    text = f"seen from {DID_A} and {UNKNOWN_DID}"

    def search(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/1", text)]

    route = make_route_route(search, None, "gen", None)
    status, body, _headers = _call(route, {"model_hash": ["m1"]})
    assert status == 200
    assert body["ledger_generated_at"] is None
    dids = body["observations"][0]["dids"]
    assert [d["did"] for d in dids] == [DID_A, UNKNOWN_DID]
    assert all(d["ledger"] == {"error": "ledger_not_built"} for d in dids)


def test_ledger_loaded_known_and_unknown_did_in_same_hit(tmp_path: Path) -> None:
    root = install_fixture(tmp_path / "log")
    ledger, _report = build_ledger(root, now=NOW)
    ledger_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, ledger_path)
    compact = load_compact_ledger(ledger_path)

    text = f"a hit mentioning {DID_A} (known) and {UNKNOWN_DID} (never posted)"

    def search(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/1", text)]

    route = make_route_route(search, compact.lookup, "gen", compact.generated_at)
    status, body, _headers = _call(route, {"model_hash": ["m1"]})
    assert status == 200
    assert body["ledger_generated_at"] == compact.generated_at

    dids_by_id = {d["did"]: d["ledger"] for d in body["observations"][0]["dids"]}
    assert dids_by_id[DID_A] == compact.lookup(DID_A)
    assert dids_by_id[DID_A]["did"] == DID_A
    assert "error" not in dids_by_id[DID_A]
    assert dids_by_id[UNKNOWN_DID] == {"error": "unknown_did"}


def test_ledger_body_byte_identical_to_did_route(tmp_path: Path) -> None:
    """The `ledger` object inside an observation's `dids[]` entry equals `/did/{did}`'s own body
    for the SAME did, from the SAME loaded ledger -- byte-identical, never re-derived."""
    root = install_fixture(tmp_path / "log")
    ledger, _report = build_ledger(root, now=NOW)
    ledger_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, ledger_path)
    compact = load_compact_ledger(ledger_path)

    did_route = make_did_prefix_route(compact)
    did_status, did_body, _headers = did_route(DID_A, {})
    assert did_status == 200

    def search(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/1", f"mentions {DID_A}")]

    route = make_route_route(search, compact.lookup, "gen", compact.generated_at)
    _status, body, _headers = _call(route, {"model_hash": ["m1"]})
    observed_ledger_body = body["observations"][0]["dids"][0]["ledger"]
    assert observed_ledger_body == did_body

    # And the burst DID too.
    did_status_b, did_body_b, _headers_b = did_route(DID_B, {})
    assert did_status_b == 200

    def search_b(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/2", f"mentions {DID_B}")]

    route_b = make_route_route(search_b, compact.lookup, "gen", compact.generated_at)
    _status_b, body_b, _headers_bb = _call(route_b, {"model_hash": ["m1"]})
    assert body_b["observations"][0]["dids"][0]["ledger"] == did_body_b


def test_x_ledger_generated_at_header_present_only_when_ledger_loaded() -> None:
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return []

    no_ledger_route = make_route_route(search, None, "gen", None)
    _status, _body, headers = _call(no_ledger_route, {"model_hash": ["m1"]})
    assert headers == {}  # nothing to report -- no ledger loaded

    with_ledger_route = make_route_route(search, lambda _did: None, "gen", "2026-09-16T05:20:00Z")
    _status2, _body2, headers2 = _call(with_ledger_route, {"model_hash": ["m1"]})
    assert headers2 == {"X-Ledger-Generated-At": "2026-09-16T05:20:00Z"}


def test_x_ledger_generated_at_header_present_even_on_a_400() -> None:
    # Same convention openagentsearch.api.did uses: present whenever a ledger is loaded, even
    # when the request itself is rejected.
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return []

    with_ledger_route = make_route_route(search, lambda _did: None, "gen", "2026-09-16T05:20:00Z")
    status, body, headers = _call(with_ledger_route, {})
    assert status == 400
    assert body == {"error": "missing_model_hash", "field": "model_hash"}
    assert headers == {"X-Ledger-Generated-At": "2026-09-16T05:20:00Z"}

    no_ledger_route = make_route_route(search, None, "gen", None)
    status2, _body2, headers2 = _call(no_ledger_route, {})
    assert status2 == 400
    assert headers2 == {}


def test_hit_with_no_dids_gets_an_empty_dids_list() -> None:
    def search(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/1", "nothing did-shaped in here")]

    route = make_route_route(search, None, "gen", None)
    _status, body, _headers = _call(route, {"model_hash": ["m1"]})
    assert body["observations"][0]["dids"] == []
    assert body["observations"][0]["url"] == "https://example.test/1"
    assert body["observations"][0]["kind"] is None


# --- real server, through create_server (like tests/test_api_did.py) --------------------------


def _get(url: str) -> tuple[int, dict[str, Any], Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), exc.headers


def test_route_over_real_http_server(tmp_path: Path) -> None:
    root = install_fixture(tmp_path / "log")
    ledger, _report = build_ledger(root, now=NOW)
    ledger_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, ledger_path)
    compact = load_compact_ledger(ledger_path)

    def search(_q: str, _k: int) -> list[ObservationHit]:
        return [_hit("https://example.test/1", f"model seen from {DID_A}", kind=None, score=3.5)]

    route = make_route_route(search, compact.lookup, "2026-09-17T00:00:00Z", compact.generated_at)
    server = create_server("127.0.0.1", 0, routes={"/route": route})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body, headers = _get(base + "/route?model_hash=m1")
        assert status == 200
        assert headers["X-Ledger-Generated-At"] == compact.generated_at
        assert body["candidates"] == []
        assert body["ranking"] is None
        assert body["observations"][0]["dids"][0]["did"] == DID_A
        assert body["observations"][0]["dids"][0]["ledger"]["did"] == DID_A

        status, body, _headers = _get(base + "/route")
        assert status == 400
        assert body == {"error": "missing_model_hash", "field": "model_hash"}

        status, body, _headers = _get(base + "/route?model_hash=" + "a" * 129)
        assert status == 400
        assert body == {"error": "invalid_model_hash", "field": "model_hash"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


# --- worker/src/offer-shape.json is generated, never stale ------------------------------------


def test_offer_shape_json_matches_a_fresh_generation() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_offer_shape_json.py"), "--check"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True


def test_offer_shape_json_matches_offer_shape_status_field_for_field() -> None:
    path = REPO / "worker" / "src" / "offer-shape.json"
    obj = json.loads(path.read_text(encoding="utf-8"))
    shape = offer_shape_status()
    assert obj == {
        "published": shape.published,
        "source": shape.source,
        "watch": list(shape.watch),
        "binds": list(shape.binds),
    }
