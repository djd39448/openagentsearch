"""A deliberately minimal MCP server: JSON-RPC 2.0 over stdio, one tool ("search") backed by GET /search.

Supported methods: initialize, tools/list, tools/call. One JSON object per input line, exactly one
compact response line per request. This is a subset of MCP; it does not claim broader compatibility.
The HTTP base URL is supplied on the command line only - never from the environment, never with a
fallback host.

Run:  python -m openagentsearch.mcp.server --base-url http://127.0.0.1:<port>
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, TextIO

PROTOCOL_VERSION_DEFAULT = "minimal-stdio-1"
SERVER_INFO = {"name": "openagentsearch", "version": "0"}
SEARCH_TOOL: Dict[str, Any] = {
    "name": "search",
    "description": "Search the local OpenAgentSearch index.",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 50}},
        "required": ["q"],
        "additionalProperties": False,
    },
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class MCPServer:
    def __init__(self, base_url: str, http_timeout: float = 5.0) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.http_timeout = http_timeout

    # ----- JSON-RPC envelopes -------------------------------------------------------------
    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ----- HTTP side ------------------------------------------------------------------------
    def search(self, q: str, k: int = 10) -> Dict[str, Any]:
        """GET <base_url>/search?q=...&k=... and return the parsed body (raises on malformed bodies)."""
        url = f"{self.base_url}/search?{urllib.parse.urlencode({'q': q, 'k': str(k)})}"
        with urllib.request.urlopen(url, timeout=self.http_timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if (
            not isinstance(body, dict)
            or not all(key in body for key in ("query", "k", "results"))
            or not isinstance(body["results"], list)
        ):
            raise ValueError("malformed /search response")
        return body

    # ----- dispatch -------------------------------------------------------------------------
    def handle(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "Invalid Request")
        request_id = message.get("id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self._error(request_id, INVALID_REQUEST, "Invalid Request")
        method = message["method"]
        params = message.get("params")
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if isinstance(requested, str) else PROTOCOL_VERSION_DEFAULT
            return self._result(
                request_id,
                {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": dict(SERVER_INFO)},
            )
        if method == "tools/list":
            return self._result(request_id, {"tools": [SEARCH_TOOL]})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, METHOD_NOT_FOUND, "Method not found")

    def _call_tool(self, request_id: Any, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict) or params.get("name") != "search" or not isinstance(params.get("arguments"), dict):
            return self._error(request_id, INVALID_PARAMS, "Invalid params")
        arguments = params["arguments"]
        if set(arguments) - {"q", "k"}:
            return self._error(request_id, INVALID_PARAMS, "Invalid params")
        q = arguments.get("q")
        if not isinstance(q, str) or not q.strip():
            return self._error(request_id, INVALID_PARAMS, "Invalid params")
        k = arguments.get("k", 10)
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 50:
            return self._error(request_id, INVALID_PARAMS, "Invalid params")

        try:
            body = self.search(q, k)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code <= 499:
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                except ValueError:
                    return self._error(request_id, INTERNAL_ERROR, "Internal error")
                return self._result(
                    request_id,
                    {"content": [{"type": "text", "text": _compact(payload)}], "structuredContent": payload, "isError": True},
                )
            return self._error(request_id, INTERNAL_ERROR, "Internal error")
        except (urllib.error.URLError, OSError, ValueError):
            return self._error(request_id, INTERNAL_ERROR, "Internal error")
        return self._result(
            request_id,
            {"content": [{"type": "text", "text": _compact(body)}], "structuredContent": body, "isError": False},
        )

    # ----- stdio loop -----------------------------------------------------------------------
    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        for raw in input_stream:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = self._error(None, PARSE_ERROR, "Parse error")
            else:
                try:
                    response = self.handle(message)
                except Exception:  # one bad request must never terminate the process
                    request_id = message.get("id") if isinstance(message, dict) else None
                    response = self._error(request_id, INTERNAL_ERROR, "Internal error")
            output_stream.write(_compact(response) + "\n")
            output_stream.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="openagentsearch.mcp.server")
    parser.add_argument("--base-url", required=True, help="HTTP base URL of the search API, e.g. http://127.0.0.1:8080")
    parser.add_argument("--http-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    MCPServer(args.base_url, args.http_timeout).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
