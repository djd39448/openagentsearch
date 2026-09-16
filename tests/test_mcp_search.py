"""P5.2 / C3: the stdio MCP server, driven as a real subprocess against real in-process HTTP
fixtures -- /search (P5.2) and /did/{did} (C3, `did_lookup`)."""

import http.server
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.mcp.server import DID_LOOKUP_TOOL, SEARCH_TOOL
from openagentsearch.vector.store import VectorStore

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 15.0


class StubEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


def _start_http(tmpdir: str):
    store = VectorStore(Path(tmpdir) / "v.sqlite3", 2)
    store.add("along", "doc-1", [1.0, 0.0], "alpha text")
    store.add("across", "doc-2", [0.0, 1.0], "orthogonal text")
    store.add("between", "doc-1", [1.0, 1.0], "diagonal text")
    embedder = StubEmbedder()
    server = create_server("127.0.0.1", 0, routes={"/search": make_search_route(store, embedder, lambda sha: f"u:{sha}")})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, store, embedder, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_http(server, thread, store) -> None:
    server.shutdown()
    server.server_close()
    thread.join()
    store.close()


class MCPClient:
    """Launches the MCP server as a subprocess; every read has a timeout via a reader thread."""

    def __init__(self, base_url: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openagentsearch.mcp.server", "--base-url", base_url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(REPO), env=env,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        self.next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def send_raw(self, line: str) -> dict:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.lines.get(timeout=TIMEOUT))

    def call(self, method: str, params=None) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        response = self.send_raw(json.dumps(message))
        assert response["jsonrpc"] == "2.0" and response["id"] == self.next_id, response
        return response

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            try:
                self.proc.communicate(timeout=5)
            except Exception:
                self.proc.kill()


def _http_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_initialize_and_tools_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, store, _, base = _start_http(tmpdir)
        client = MCPClient(base)
        try:
            init = client.call("initialize", {"protocolVersion": "x-test-1", "capabilities": {}})
            assert init["result"]["protocolVersion"] == "x-test-1"
            assert init["result"]["capabilities"] == {"tools": {}}
            assert init["result"]["serverInfo"] == {"name": "openagentsearch", "version": "0"}
            default = client.call("initialize")
            assert default["result"]["protocolVersion"] == "minimal-stdio-1"
            listed = client.call("tools/list")
            assert listed["result"] == {"tools": [SEARCH_TOOL, DID_LOOKUP_TOOL]}
            assert len(listed["result"]["tools"]) == 2
            assert [tool["name"] for tool in listed["result"]["tools"]] == ["search", "did_lookup"]
            assert listed["result"]["tools"][0]["inputSchema"] == {
                "type": "object",
                "properties": {"q": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 50}},
                "required": ["q"],
                "additionalProperties": False,
            }
            assert listed["result"]["tools"][1]["inputSchema"] == {
                "type": "object",
                "properties": {"did": {"type": "string", "pattern": "^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$"}},
                "required": ["did"],
            }
        finally:
            client.close()
            _stop_http(server, thread, store)


def test_tools_call_matches_direct_http_search():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, store, _, base = _start_http(tmpdir)
        client = MCPClient(base)
        try:
            direct = _http_get(base + "/search?q=alpha&k=2")
            response = client.call("tools/call", {"name": "search", "arguments": {"q": "alpha", "k": 2}})
            result = response["result"]
            assert result["isError"] is False
            assert result["structuredContent"] == direct
            assert [r["chunk_id"] for r in result["structuredContent"]["results"]] == ["along", "between"]
            assert result["content"] == [{"type": "text", "text": json.dumps(direct, separators=(",", ":"), ensure_ascii=False)}]
            assert json.loads(result["content"][0]["text"]) == direct
        finally:
            client.close()
            _stop_http(server, thread, store)


def test_invalid_tool_calls_are_rejected_without_http():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, store, embedder, base = _start_http(tmpdir)
        client = MCPClient(base)
        try:
            baseline = list(embedder.calls)
            bad_params = [
                {"name": "search", "arguments": {}},
                {"name": "search", "arguments": {"q": "   "}},
                {"name": "search", "arguments": {"q": "alpha", "k": True}},
                {"name": "search", "arguments": {"q": "alpha", "k": 0}},
                {"name": "search", "arguments": {"q": "alpha", "k": 51}},
                {"name": "search", "arguments": {"q": "alpha", "extra": 1}},
                {"name": "other", "arguments": {"q": "alpha"}},
                {"name": "search"},
                "not-an-object",
            ]
            for params in bad_params:
                response = client.call("tools/call", params)
                assert response["error"] == {"code": -32602, "message": "Invalid params"}, params
            assert embedder.calls == baseline  # nothing reached /search
        finally:
            client.close()
            _stop_http(server, thread, store)


def test_malformed_lines_and_unknown_methods_do_not_kill_the_server():
    with tempfile.TemporaryDirectory() as tmpdir:
        server, thread, store, _, base = _start_http(tmpdir)
        client = MCPClient(base)
        try:
            parse = client.send_raw("{this is not json")
            assert parse == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            array = client.send_raw("[1, 2, 3]")
            assert array["error"] == {"code": -32600, "message": "Invalid Request"}
            missing_version = client.send_raw(json.dumps({"id": 7, "method": "tools/list"}))
            assert missing_version["id"] == 7 and missing_version["error"]["code"] == -32600
            unknown = client.call("nope/method")
            assert unknown["error"] == {"code": -32601, "message": "Method not found"}
            still_alive = client.call("tools/list")
            assert still_alive["result"]["tools"][0]["name"] == "search"
        finally:
            client.close()
            _stop_http(server, thread, store)


def test_unreachable_base_url_is_an_internal_error_with_no_fallback():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    client = MCPClient(f"http://127.0.0.1:{closed_port}")
    try:
        listed = client.call("tools/list")
        assert listed["result"]["tools"][0]["name"] == "search"  # listing needs no HTTP
        response = client.call("tools/call", {"name": "search", "arguments": {"q": "alpha", "k": 1}})
        assert response["error"] == {"code": -32603, "message": "Internal error"}
        assert client.call("tools/list")["result"]["tools"][0]["name"] == "search"  # still serving
    finally:
        client.close()


# --- C3: did_lookup ---------------------------------------------------------------------------
# Fixture did:key values, each matching DID_PATTERN (`^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$`,
# same shape docs/api.md documents for the public /did/{did} route). Only the trailing character
# differs between them, so each stays a valid base58 suffix (never '0', 'O', 'I', or lowercase
# 'l').
FIXTURE_DID_OK = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"
FIXTURE_DID_NOT_BUILT = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuiz2"
FIXTURE_DID_REJECTED = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuiz3"
FIXTURE_DID_BAD_JSON = "did:key:z6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuiz4"
# Fails DID_PATTERN locally (no "z" multibase prefix after "did:key:") -- must never reach a server.
INVALID_DID = "did:key:6MkfVWRHNeiV99ckgHDmi8HpwMLtir1XsTu9rNCoYdTuizf"

FIXTURE_DID_BODY = {
    "did": FIXTURE_DID_OK,
    "score": 0.87,
    "facts": ["registered"],
    "provenance": {"source": "fixture-ledger"},
}

DID_RESPONSES = {
    FIXTURE_DID_OK: (200, "application/json; charset=utf-8", json.dumps(FIXTURE_DID_BODY).encode("utf-8")),
    FIXTURE_DID_NOT_BUILT: (404, "application/json; charset=utf-8", b'{"error":"ledger_not_built"}'),
    FIXTURE_DID_REJECTED: (400, "application/json; charset=utf-8", b'{"error":"invalid_did"}'),
    # A 200 whose body is not valid JSON at all -- the "non-JSON body" case from the spec's test
    # list. A raw http.server.BaseHTTPRequestHandler is used (not create_server, which always
    # JSON-encodes its route's return value) so an actually-malformed body can be sent on the wire.
    FIXTURE_DID_BAD_JSON: (200, "text/plain; charset=utf-8", b"not json at all"),
}


def _start_did_http(responses: dict):
    """A loopback HTTP server answering GET /did/<did> from a canned {did: (status, content_type,
    body_bytes)} map; any other did answers 404 not_found. `seen` records every requested path
    (percent-encoded, as sent on the wire) in order -- used to assert a locally-rejected did never
    reaches the server at all."""
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            prefix = "/did/"
            if not self.path.startswith(prefix):
                status, content_type, body = 404, "application/json", b'{"error":"not_found"}'
            else:
                did = urllib.parse.unquote(self.path[len(prefix):])
                status, content_type, body = responses.get(
                    did, (404, "application/json", b'{"error":"not_found"}')
                )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, log_format: str, *args: object) -> None:
            pass  # keep test output quiet

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, seen, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_did_http(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join()


def test_did_lookup_maps_every_documented_status_and_percent_encodes_the_path():
    server, thread, seen, base = _start_did_http(DID_RESPONSES)
    client = MCPClient(base)
    try:
        ok = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_OK}})
        assert ok["result"]["isError"] is False
        assert ok["result"]["structuredContent"] == FIXTURE_DID_BODY
        assert ok["result"]["content"] == [
            {"type": "text", "text": json.dumps(FIXTURE_DID_BODY, separators=(",", ":"), ensure_ascii=False)}
        ]

        not_built = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_NOT_BUILT}})
        assert not_built["result"]["isError"] is True
        assert not_built["result"]["structuredContent"] == {"error": "ledger_not_built"}
        assert json.loads(not_built["result"]["content"][0]["text"]) == {"error": "ledger_not_built"}

        rejected = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_REJECTED}})
        assert rejected["result"]["isError"] is True
        assert rejected["result"]["structuredContent"] == {"error": "invalid_did"}

        bad_json = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_BAD_JSON}})
        assert bad_json["result"]["isError"] is True
        assert "structuredContent" not in bad_json["result"]  # nothing parsed -- one-line reason only
        reason = bad_json["result"]["content"][0]["text"]
        assert bad_json["result"]["content"][0]["type"] == "text"
        assert reason and "not json at all" not in reason  # the raw bad body is never echoed back

        # Every request landed at the percent-encoded /did/<did> path, in order, and nowhere else.
        requested_dids = [urllib.parse.unquote(path[len("/did/"):]) for path in seen]
        assert requested_dids == [
            FIXTURE_DID_OK,
            FIXTURE_DID_NOT_BUILT,
            FIXTURE_DID_REJECTED,
            FIXTURE_DID_BAD_JSON,
        ]
        assert all(":" not in path for path in seen)  # ':' was percent-encoded, not sent raw
    finally:
        client.close()
        _stop_did_http(server, thread)


def test_did_lookup_rejects_an_invalid_did_locally_with_zero_requests():
    server, thread, seen, base = _start_did_http(DID_RESPONSES)
    client = MCPClient(base)
    try:
        response = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": INVALID_DID}})
        assert response["result"]["isError"] is True
        assert response["result"]["structuredContent"] == {"error": "invalid_did"}
        assert json.loads(response["result"]["content"][0]["text"]) == {"error": "invalid_did"}
        assert seen == []  # the malformed did never reached the server
    finally:
        client.close()
        _stop_did_http(server, thread)


def test_did_lookup_against_a_closed_port_is_a_tool_error_not_a_protocol_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    client = MCPClient(f"http://127.0.0.1:{closed_port}")
    try:
        response = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_OK}})
        assert "error" not in response  # not a JSON-RPC protocol error, unlike search's mapping
        assert response["result"]["isError"] is True
        assert "structuredContent" not in response["result"]
        reason = response["result"]["content"][0]["text"]
        assert response["result"]["content"][0]["type"] == "text" and reason
        # one bad tool call does not kill the server
        assert [tool["name"] for tool in client.call("tools/list")["result"]["tools"]] == ["search", "did_lookup"]
    finally:
        client.close()

def test_did_lookup_never_follows_a_redirect():
    """A /did/{did} answer that redirects is a tool error, not a hop to chase: the server sees
    exactly one request and the Location target (an unreachable port) is never contacted."""
    seen: list = []
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    unreachable_port = probe.getsockname()[1]
    probe.close()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{unreachable_port}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, log_format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = MCPClient(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        response = client.call("tools/call", {"name": "did_lookup", "arguments": {"did": FIXTURE_DID_OK}})
        assert "error" not in response
        assert response["result"]["isError"] is True
        assert "HTTP 302" in response["result"]["content"][0]["text"]
        assert seen == [f"/did/{urllib.parse.quote(FIXTURE_DID_OK, safe='')}"]
    finally:
        client.close()
        _stop_did_http(server, thread)

