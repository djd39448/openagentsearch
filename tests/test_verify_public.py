"""Package C2b, spec Tests item 7: `scripts/verify_public.py` against a loopback `http.server`
standing in for a deployed Worker -- never a real network call. Every subprocess call carries a
timeout so a bug here can never hang the suite; the server is always stopped in `finally`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "verify_public.py"
SUBPROCESS_TIMEOUT = 30

HEALTHZ_OK: dict[str, Any] = {
    "status": "ok",
    "index": {"indexed": 3, "failed": 0, "superseded": 0, "refused": 0},
    "kinds": {"room": 2, "site": 1},
    "generated_at": "2026-01-01T00:00:00Z",
    "db_sha256": "0" * 64,
    "lexical": {"docs": 3, "terms": 10, "postings": 12},
}

MANIFEST_OK: dict[str, Any] = {
    "schema": "openagentsearch.static-index/1",
    "generated_at": "2026-01-01T00:00:00Z",
    "db_sha256": "0" * 64,
    "counts": {"indexed": 3, "failed": 0, "superseded": 0, "refused": 0},
    "kinds": {
        "room": {"indexed": 2, "failed": 0, "superseded": 0, "refused": 0},
        "site": {"indexed": 1, "failed": 0, "superseded": 0, "refused": 0},
    },
    "documents": [],
}

TOOLS_LIST_OK = ["did_lookup", "index_info", "search"]

# Package B2 -- --ledger fixtures. OUR_DID matches scripts/verify_public.py's own OUR_DID.
OUR_DID = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"

LOCAL_LEDGER_OK: dict[str, Any] = {
    "schema": "openagentsearch.did-ledger-compact/1",
    "generated_at": "2026-02-02T00:00:00Z",
    "log_rows": 50,
    "posts": 40,
    "dids": 5,
    "bursts": 0,
    "non_burst": {OUR_DID: {"facts": {"first_seen_seq": 3}, "score": {}}},
    "burst": {},
}

HEALTHZ_WITH_LEDGER_OK: dict[str, Any] = {
    **HEALTHZ_OK,
    "ledger": {"dids": 5, "bursts": 0, "generated_at": "2026-02-02T00:00:00Z"},
}

DID_BODY_OK: dict[str, Any] = {
    "did": OUR_DID,
    "burst": False,
    "score": 1.5,
    "facts_used": [],
    "facts": {"first_seen_seq": 3},
    "provenance": {},
}


def _rpc_ok(request_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_handler(
    *,
    healthz_status: int = 200,
    healthz_body: bytes | None = None,
    healthz_content_type: str = "application/json",
    healthz_location: str | None = None,
    mcp_responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    did_status: int = 200,
    did_body: dict[str, Any] | None = None,
) -> type[BaseHTTPRequestHandler]:
    resolved_healthz_body: bytes = (
        healthz_body if healthz_body is not None else json.dumps(HEALTHZ_OK).encode("utf-8")
    )
    resolved_did_body: bytes = json.dumps(did_body if did_body is not None else {}).encode(
        "utf-8"
    )

    def default_mcp_responder(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("method") == "initialize":
            return _rpc_ok(
                message["id"],
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "openagentsearch", "version": "0.0.0"},
                },
            )
        if message.get("method") == "tools/list":
            return _rpc_ok(
                message["id"],
                {"tools": [{"name": name} for name in TOOLS_LIST_OK]},
            )
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": "unknown"},
        }

    responder = mcp_responder or default_mcp_responder

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, log_format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/healthz":
                if healthz_location is not None:
                    self.send_response(302)
                    self.send_header("Location", healthz_location)
                    self.end_headers()
                    return
                self.send_response(healthz_status)
                self.send_header("Content-Type", healthz_content_type)
                self.end_headers()
                self.wfile.write(resolved_healthz_body)
                return
            if self.path.startswith("/did/"):
                self.send_response(did_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resolved_did_body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            message = json.loads(raw.decode("utf-8"))
            reply = responder(message)
            body = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class _Server:
    """A loopback `http.server.HTTPServer` on a background thread, stopped in `finally`."""

    def __init__(self, handler_cls: type[BaseHTTPRequestHandler]) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def _run(
    base_url: str, manifest_path: Path, *, ledger_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable, str(SCRIPT), base_url, "--manifest", str(manifest_path), "--timeout", "5",
    ]
    if ledger_path is not None:
        argv += ["--ledger", str(ledger_path)]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )


def _write_manifest(tmp_path: Path, manifest: dict[str, Any] = MANIFEST_OK) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _write_ledger(tmp_path: Path, ledger: dict[str, Any] = LOCAL_LEDGER_OK) -> Path:
    path = tmp_path / "ledger-compact.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    return path


def test_match_exits_0(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    with _Server(_make_handler()) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["indexed"] == 3
    assert report["tools"] == TOOLS_LIST_OK


def test_count_mismatch_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    bad_healthz = dict(HEALTHZ_OK)
    bad_healthz["index"] = {"indexed": 999, "failed": 0, "superseded": 0, "refused": 0}
    handler = _make_handler(healthz_body=json.dumps(bad_healthz).encode("utf-8"))
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert "index.indexed mismatch" in report["reason"]


def test_kind_count_mismatch_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    bad_healthz = dict(HEALTHZ_OK)
    bad_healthz["kinds"] = {"room": 1, "site": 1}
    handler = _make_handler(healthz_body=json.dumps(bad_healthz).encode("utf-8"))
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "kinds mismatch" in report["reason"]


def test_missing_tool_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)

    def responder(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("method") == "initialize":
            return _rpc_ok(
                message["id"],
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "openagentsearch", "version": "0.0.0"},
                },
            )
        if message.get("method") == "tools/list":
            return _rpc_ok(message["id"], {"tools": [{"name": "search"}, {"name": "index_info"}]})
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": "unknown"},
        }

    with _Server(_make_handler(mcp_responder=responder)) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "tool list mismatch" in report["reason"]


def test_non_json_healthz_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    handler = _make_handler(healthz_body=b"not json at all", healthz_content_type="text/plain")
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert "not valid JSON" in report["reason"]


def test_bad_arguments_exit_2(tmp_path: Path) -> None:
    # No --manifest at all: argparse itself refuses before any network access.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "http://127.0.0.1:1"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    handler = _make_handler(healthz_location="http://127.0.0.1:9/elsewhere")
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert "redirect" in report["reason"]


# --- Package B2: --ledger --------------------------------------------------------------------


def test_ledger_match_exits_0(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    handler = _make_handler(
        healthz_body=json.dumps(HEALTHZ_WITH_LEDGER_OK).encode("utf-8"), did_body=DID_BODY_OK
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["ledger_dids"] == 5
    assert report["first_seen_seq"] == 3


def test_ledger_did_request_uses_the_literal_unencoded_path(tmp_path: Path) -> None:
    """`_make_handler`'s `startswith("/did/")` stub (used by every other `--ledger` test above)
    matches any `/did/...` path, encoded or not, so it can never catch a wrong URL -- this test
    uses a stricter stub that answers 200 ONLY for the exact literal path `/did/<OUR_DID>` (real
    colons, no `%3A`) and 404 for anything else, the same way the real deployed Worker rejects a
    percent-encoded DID segment (it never decodes `pathname`). Regenerates the class of bug where
    `scripts/verify_public.py` percent-encoded the DID before building `did_url`.
    """
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    expected_path = f"/did/{OUR_DID}"
    did_body_bytes = json.dumps(DID_BODY_OK).encode("utf-8")
    healthz_body_bytes = json.dumps(HEALTHZ_WITH_LEDGER_OK).encode("utf-8")

    class ExactPathHandler(BaseHTTPRequestHandler):
        def log_message(self, log_format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(healthz_body_bytes)
                return
            if self.path == expected_path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(did_body_bytes)
                return
            # Any other /did/... path (in particular a percent-encoded one) is a routing miss,
            # exactly as the real Worker answers for a `did` its DID_RE rejects.
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "invalid_did"}).encode("utf-8"))

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            message = json.loads(raw.decode("utf-8"))
            if message.get("method") == "initialize":
                reply = _rpc_ok(
                    message["id"],
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "openagentsearch", "version": "0.0.0"},
                    },
                )
            else:
                reply = _rpc_ok(
                    message["id"], {"tools": [{"name": name} for name in TOOLS_LIST_OK]}
                )
            body = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    with _Server(ExactPathHandler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["first_seen_seq"] == 3


def test_ledger_dids_mismatch_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    bad_healthz = {
        **HEALTHZ_WITH_LEDGER_OK,
        "ledger": {"dids": 999, "bursts": 0, "generated_at": "2026-02-02T00:00:00Z"},
    }
    handler = _make_handler(
        healthz_body=json.dumps(bad_healthz).encode("utf-8"), did_body=DID_BODY_OK
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert "ledger dids mismatch" in report["reason"]


def test_ledger_generated_at_mismatch_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    bad_healthz = {
        **HEALTHZ_WITH_LEDGER_OK,
        "ledger": {"dids": 5, "bursts": 0, "generated_at": "2099-01-01T00:00:00Z"},
    }
    handler = _make_handler(
        healthz_body=json.dumps(bad_healthz).encode("utf-8"), did_body=DID_BODY_OK
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "ledger generated_at mismatch" in report["reason"]


def test_ledger_first_seen_seq_mismatch_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    bad_did_body = {**DID_BODY_OK, "facts": {"first_seen_seq": 999}}
    handler = _make_handler(
        healthz_body=json.dumps(HEALTHZ_WITH_LEDGER_OK).encode("utf-8"), did_body=bad_did_body
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "first_seen_seq mismatch" in report["reason"]


def test_ledger_missing_did_route_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    ledger_path = _write_ledger(tmp_path)
    handler = _make_handler(
        healthz_body=json.dumps(HEALTHZ_WITH_LEDGER_OK).encode("utf-8"),
        did_status=404,
        did_body={"error": "not_found"},
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=ledger_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    assert "HTTP 404" in report["reason"]


def test_ledger_missing_local_file_exits_1(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path)
    missing_ledger_path = tmp_path / "does-not-exist.json"
    handler = _make_handler(
        healthz_body=json.dumps(HEALTHZ_WITH_LEDGER_OK).encode("utf-8"), did_body=DID_BODY_OK
    )
    with _Server(handler) as server:
        proc = _run(server.base_url, manifest_path, ledger_path=missing_ledger_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "cannot read ledger" in report["reason"]


def test_no_ledger_flag_skips_ledger_checks(tmp_path: Path) -> None:
    # The existing (pre-B2) match case: --ledger omitted, healthz has no "ledger" key at all --
    # verify() never looks for one.
    manifest_path = _write_manifest(tmp_path)
    with _Server(_make_handler()) as server:
        proc = _run(server.base_url, manifest_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert "ledger_dids" not in report
    assert "first_seen_seq" not in report
