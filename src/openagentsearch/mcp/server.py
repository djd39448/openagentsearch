"""A deliberately minimal MCP server: JSON-RPC 2.0 over stdio, two tools ("search" backed by GET
/search, "did_lookup" backed by GET /did/{did}).

Supported methods: initialize, tools/list, tools/call. One JSON object per input line, exactly one
compact response line per request. This is a subset of MCP; it does not claim broader compatibility.
The HTTP base URL is supplied on the command line only - never from the environment, never with a
fallback host.

Run:  python -m openagentsearch.mcp.server --base-url http://127.0.0.1:<port>
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple, TextIO

PROTOCOL_VERSION_DEFAULT = "minimal-stdio-1"
SERVER_INFO = {"name": "openagentsearch", "version": "0"}
USER_AGENT = "openagentsearch-mcp/0"
MAX_DID_BODY_BYTES = 1_000_000
# Same shape the public /did/{did} route documents (docs/api.md): the did:key method, a
# multibase "z" (base58btc) prefix, 1-120 base58 characters after it.
DID_PATTERN = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$")
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
DID_LOOKUP_TOOL: Dict[str, Any] = {
    "name": "did_lookup",
    "description": (
        "Look up ledger facts, score, and provenance for one did:key identity from the public "
        "GET /did/{did} route on the configured --base-url: a full 200 body once that server was "
        "started with --ledger PATH (package B2), or that route's own ledger_not_built placeholder "
        "when it was not. This tool performs no signature or cryptographic verification of its "
        "own -- it only relays what the route says."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "did": {"type": "string", "pattern": "^did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}$"},
        },
        "required": ["did"],
    },
}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turns every redirect into the underlying `HTTPError` instead of following it -- a
    `/did/{did}` response that redirects is treated as a lookup failure, never a hop to chase."""

    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


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
        # An explicit User-Agent: Cloudflare's edge (the public Worker) answers 403 "error code:
        # 1010" to urllib's default `Python-urllib/x.y`; any explicit value passes.
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.http_timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if (
            not isinstance(body, dict)
            or not all(key in body for key in ("query", "k", "results"))
            or not isinstance(body["results"], list)
        ):
            raise ValueError("malformed /search response")
        return body

    def did_lookup(self, did: str) -> Tuple[int, Dict[str, Any]]:
        """GET <base_url>/did/<did> and return (status, parsed body) -- the ledger facts, score
        and provenance the public /did/{did} route answers for one did:key (docs/api.md).

        `did` is validated locally against DID_PATTERN first: a value that does not match never
        causes a request -- this returns (400, {"error": "invalid_did"}) synthesized locally, the
        same shape the public route itself answers for a malformed did, but without ever touching
        the network. A did that does pass this local check is percent-encoded into a single path
        segment and sent with the same explicit User-Agent as search() (Cloudflare's edge answers
        403 to urllib's default one); the request never follows a redirect, and the response body
        is read bounded at MAX_DID_BODY_BYTES, raising ValueError if that bound is exceeded. Only
        the three statuses the route documents (200, 400, 404) have their body parsed -- strictly,
        as JSON, and it must decode to a JSON object or this raises ValueError; any other status
        (a 3xx redirect answer, a 5xx, a 403 from an edge) is returned as (status, {}) with the
        body discarded, so the caller reports it by its code.

        NOT guaranteed: this performs no signature or cryptographic verification of any kind and
        makes no claim about the ledger facts' authenticity -- it only relays whatever the public
        route on the configured base_url currently says: a full 200 body once that server was
        started with --ledger PATH (package B2), or that route's own "ledger_not_built"
        placeholder when it was not.
        """
        if not DID_PATTERN.match(did):
            return 400, {"error": "invalid_did"}
        url = f"{self.base_url}/did/{urllib.parse.quote(did, safe='')}"
        opener = urllib.request.build_opener(_NoRedirectHandler)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            response = opener.open(request, timeout=self.http_timeout)
        except urllib.error.HTTPError as answered:
            response = answered
        with response:
            status = int(response.getcode() or 0)
            raw = response.read(MAX_DID_BODY_BYTES + 1)
        if len(raw) > MAX_DID_BODY_BYTES:
            raise ValueError(f"did lookup response exceeds {MAX_DID_BODY_BYTES} bytes")
        if status not in (200, 400, 404):
            return status, {}
        parsed: Any = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("malformed /did response")
        return status, parsed

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
            return self._result(request_id, {"tools": [SEARCH_TOOL, DID_LOOKUP_TOOL]})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return self._error(request_id, METHOD_NOT_FOUND, "Method not found")

    def _call_tool(self, request_id: Any, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("arguments"), dict):
            return self._error(request_id, INVALID_PARAMS, "Invalid params")
        name = params.get("name")
        arguments = params["arguments"]
        if name == "search":
            return self._call_search(request_id, arguments)
        if name == "did_lookup":
            return self._call_did_lookup(request_id, arguments)
        return self._error(request_id, INVALID_PARAMS, "Invalid params")

    def _call_search(self, request_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
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

    def _call_did_lookup(self, request_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        did = arguments.get("did")
        if not isinstance(did, str):
            return self._error(request_id, INVALID_PARAMS, "Invalid params")

        try:
            status, body = self.did_lookup(did)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._tool_error(request_id, f"did lookup failed: {exc}")

        if status == 200:
            return self._result(
                request_id,
                {"content": [{"type": "text", "text": _compact(body)}], "structuredContent": body, "isError": False},
            )
        if status in (400, 404) and "error" in body:
            # A well-formed request the route itself refused (e.g. invalid_did, caught locally
            # before any request; or ledger_not_built) -- the agent sees the server's own reason.
            return self._result(
                request_id,
                {"content": [{"type": "text", "text": _compact(body)}], "structuredContent": body, "isError": True},
            )
        return self._tool_error(request_id, f"did lookup failed: HTTP {status}")

    def _tool_error(self, request_id: Any, reason: str) -> Dict[str, Any]:
        """A tools/call result reporting a transport failure or unrecognized answer: isError:
        true with a one-line, human-readable reason. Distinct from the JSON-RPC protocol-level
        errors _error() returns -- this always resolves the request; nothing about the JSON-RPC
        envelope itself failed."""
        result = {"content": [{"type": "text", "text": reason}], "isError": True}
        return self._result(request_id, result)

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
