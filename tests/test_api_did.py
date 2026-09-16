"""Package B2, spec item 3: `/did/{did}` on a real `openagentsearch.api.server` (`--ledger`), one
real-server integration test per documented outcome -- 200 non-burst, 200 burst, 400 invalid_did,
404 unknown_did, 404 ledger_not_built (no `--ledger`), plus `/healthz`'s `ledger` counts.

Every server here is built in-process (`openagentsearch.api.cli.build_server`, the same function
`python -m openagentsearch.api.server` uses) on a background thread and driven over real loopback
HTTP -- the same pattern `tests/test_index_manifest.py`'s
`test_store_aware_healthz_over_real_server` uses, faster than a subprocess and just as real a
server.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from openagentsearch.api.cli import _build_parser, build_server
from openagentsearch.reputation.compact import SCHEMA_COMPACT, write_compact_ledger
from openagentsearch.reputation.ledger import build_ledger
from reputation_fixture_support import DID_A, DID_B, NOW, install_fixture

HTTP_TIMEOUT = 5.0
UNKNOWN_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"


@dataclass
class _LiveServer:
    base_url: str
    server: Any
    store: Any
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.store.close()


def _start_server(tmp_path: Path, *, ledger_path: Path | None = None) -> _LiveServer:
    argv = [
        "--db", str(tmp_path / "v.sqlite"),
        "--embedder", "keyword",
        "--host", "127.0.0.1",
        "--port", "0",
    ]
    if ledger_path is not None:
        argv += ["--ledger", str(ledger_path)]
    args = _build_parser().parse_args(argv)
    args.dimension = 256
    server, store = build_server(args)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return _LiveServer(base_url=f"http://{host}:{port}", server=server, store=store, thread=thread)


@pytest.fixture
def compact_ledger_path(tmp_path: Path) -> Path:
    root = install_fixture(tmp_path / "log")
    ledger, _report = build_ledger(root, now=NOW)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)
    return out_path


def _get(url: str) -> tuple[int, dict[str, Any], Any]:
    """GET `url`, returning `(status, json_body, headers)`."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, response.headers
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body, exc.headers


# --- 404 ledger_not_built: no --ledger given ----------------------------------------------------


def test_did_route_with_no_ledger_is_404_ledger_not_built(tmp_path: Path) -> None:
    live = _start_server(tmp_path)
    try:
        status, body, headers = _get(f"{live.base_url}/did/{UNKNOWN_DID}")
        assert status == 404
        assert body == {"error": "ledger_not_built"}
        assert "X-Ledger-Generated-At" not in headers
    finally:
        live.stop()


def test_healthz_ledger_is_null_with_no_ledger_given(tmp_path: Path) -> None:
    live = _start_server(tmp_path)
    try:
        status, body, _headers = _get(f"{live.base_url}/healthz")
        assert status == 200
        assert body["ledger"] is None
    finally:
        live.stop()


# --- 400 invalid_did -----------------------------------------------------------------------------


def test_did_route_malformed_did_is_400(tmp_path: Path, compact_ledger_path: Path) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        status, body, headers = _get(f"{live.base_url}/did/not-a-valid-did")
        assert status == 400
        assert body == {"error": "invalid_did"}
        # Present even for a 400: a ledger IS loaded, so there is something to report.
        assert headers["X-Ledger-Generated-At"] is not None
    finally:
        live.stop()


# --- 200: known non-burst DID ---------------------------------------------------------------------


def test_did_route_known_non_burst_did_is_200_with_full_body(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        status, body, headers = _get(f"{live.base_url}/did/{DID_A}")
        assert status == 200
        assert headers["X-Ledger-Generated-At"] is not None
        assert body["did"] == DID_A
        assert body["burst"] is False
        assert "burst_id" not in body
        assert isinstance(body["score"], (int, float))
        assert isinstance(body["facts_used"], list) and body["facts_used"]
        assert body["facts"]["first_seen_seq"] is not None
        assert body["provenance"]["schema"] == SCHEMA_COMPACT
        assert body["provenance"]["dids"] == 20
    finally:
        live.stop()


# --- 200: known burst DID -------------------------------------------------------------------------


def test_did_route_known_burst_did_is_200_with_compact_body(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        status, body, _headers = _get(f"{live.base_url}/did/{DID_B}")
        assert status == 200
        assert body["did"] == DID_B
        assert body["burst"] is True
        assert body["score"] == 0.0
        assert body["facts"] is None
        assert [pair[0] for pair in body["facts_used"]] == [
            "burst", "first_seen_ts", "post_count", "max_posts_per_minute",
        ]
        # This fixture's DID_B has a whole-second (integral) first_seen_ts -- assert the actual
        # value, not just the key order, so a `str(float)`-vs-JS-`String()` formatting divergence
        # (a trailing ".0" Python keeps and the Worker/MCP side never produces for the same JSON
        # value -- `handoff/B2-SPEC.md` item 2's "one shape everywhere") cannot regress silently.
        first_seen_ts_pair = next(p for p in body["facts_used"] if p[0] == "first_seen_ts")
        assert "." not in first_seen_ts_pair[1]
        assert "burst_id" in body
    finally:
        live.stop()


# --- 404: unknown well-formed DID -----------------------------------------------------------------


def test_did_route_unknown_well_formed_did_is_404_unknown_did(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        status, body, headers = _get(f"{live.base_url}/did/{UNKNOWN_DID}")
        assert status == 404
        assert body == {"error": "unknown_did"}
        assert headers["X-Ledger-Generated-At"] is not None
    finally:
        live.stop()


# --- percent-encoded segment ---------------------------------------------------------------------


def test_did_route_percent_encoded_segment_is_decoded(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        encoded = urllib.parse.quote(DID_A, safe="")
        status, body, _headers = _get(f"{live.base_url}/did/{encoded}")
        assert status == 200
        assert body["did"] == DID_A
    finally:
        live.stop()


# --- /healthz reports ledger counts -----------------------------------------------------------


def test_healthz_reports_ledger_counts_when_ledger_given(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        status, body, _headers = _get(f"{live.base_url}/healthz")
        assert status == 200
        assert body["ledger"]["dids"] == 20
        assert body["ledger"]["bursts"] == 0
        assert isinstance(body["ledger"]["generated_at"], str)
    finally:
        live.stop()


# --- HEAD --------------------------------------------------------------------------------------


def test_head_did_route_matches_get_status_and_headers(
    tmp_path: Path, compact_ledger_path: Path
) -> None:
    live = _start_server(tmp_path, ledger_path=compact_ledger_path)
    try:
        request = urllib.request.Request(f"{live.base_url}/did/{DID_A}", method="HEAD")
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            assert response.status == 200
            assert response.headers["X-Ledger-Generated-At"] is not None
            assert response.read() == b""
    finally:
        live.stop()
