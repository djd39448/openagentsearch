"""P5.2: the stdio MCP server, driven as a real subprocess against a real in-process HTTP /search."""

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.mcp.server import SEARCH_TOOL
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
            assert listed["result"] == {"tools": [SEARCH_TOOL]}
            assert len(listed["result"]["tools"]) == 1
            assert listed["result"]["tools"][0]["inputSchema"] == {
                "type": "object",
                "properties": {"q": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 50}},
                "required": ["q"],
                "additionalProperties": False,
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
