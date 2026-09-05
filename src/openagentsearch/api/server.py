import http.server
import json
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler
from typing import Any

JSONRoute = Callable[[dict[str, list[str]]], tuple[int, dict[str, object]]]
PrefixJSONRoute = Callable[[str, dict[str, list[str]]], tuple[int, dict[str, object]]]


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    routes: Mapping[str, JSONRoute] | None = None,
    prefix_routes: Mapping[str, PrefixJSONRoute] | None = None,
) -> http.server.ThreadingHTTPServer:
    """Create a ThreadingHTTPServer with JSON API routes."""
    
    # Default healthz route
    default_routes: dict[str, JSONRoute] = {
        "/healthz": lambda query_dict: (200, {"status": "ok"})
    }
    
    # Merge with user-provided routes
    if routes:
        default_routes.update(routes)
    
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # Parse the path and query parameters
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            
            # Look up exact route first (exact match takes precedence)
            route_func = default_routes.get(path)
            
            if route_func is not None:
                # Exact match found
                status, data = route_func(query_dict)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(response_data)))
                self.end_headers()
                self.wfile.write(response_data)
            else:
                # Try prefix routes (if any are defined). Both names must exist even when no
                # prefix routes were registered, or the check below raises UnboundLocalError
                # and the connection is dropped (this broke GET /nope on 41407e2).
                matched_prefix = None
                matched_handler = None
                if prefix_routes:
                    # Find the longest matching prefix
                    for prefix, handler in prefix_routes.items():
                        if path.startswith(prefix):
                            # Keep the longest prefix that matches
                            if matched_prefix is None or len(prefix) > len(matched_prefix):
                                matched_prefix = prefix
                                matched_handler = handler
                
                # If we found a matching prefix route, call it
                if matched_handler:
                    remainder = path[len(matched_prefix):]
                    status, data = matched_handler(remainder, query_dict)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(response_data)))
                    self.end_headers()
                    self.wfile.write(response_data)
                else:
                    # No exact or prefix route found
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    response_data = json.dumps({"error": "not_found"}, separators=(",", ":")).encode("utf-8")
                    self.send_header("Content-Length", str(len(response_data)))
                    self.end_headers()
                    self.wfile.write(response_data)
        
        def do_HEAD(self) -> None:
            # Parse the path and query parameters
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            
            # Look up exact route first (exact match takes precedence)
            route_func = default_routes.get(path)
            
            if route_func is not None:
                # Exact match found
                status, data = route_func(query_dict)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(response_data)))
                self.end_headers()
            else:
                # Try prefix routes (if any are defined). Both names must exist even when no
                # prefix routes were registered, or the check below raises UnboundLocalError
                # and the connection is dropped (this broke GET /nope on 41407e2).
                matched_prefix = None
                matched_handler = None
                if prefix_routes:
                    # Find the longest matching prefix
                    for prefix, handler in prefix_routes.items():
                        if path.startswith(prefix):
                            # Keep the longest prefix that matches
                            if matched_prefix is None or len(prefix) > len(matched_prefix):
                                matched_prefix = prefix
                                matched_handler = handler
                
                # If we found a matching prefix route, call it
                if matched_handler:
                    remainder = path[len(matched_prefix):]
                    status, data = matched_handler(remainder, query_dict)
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(response_data)))
                    self.end_headers()
                else:
                    # No exact or prefix route found
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    response_data = json.dumps({"error": "not_found"}, separators=(",", ":")).encode("utf-8")
                    self.send_header("Content-Length", str(len(response_data)))
                    self.end_headers()
        
        def _method_not_allowed(self) -> None:
            # Never read or execute request content; the body is the exact documented 405 contract.
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            response_data = json.dumps({"error": "method_not_allowed"}, separators=(",", ":")).encode("utf-8")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)

        def do_POST(self) -> None:
            # Reject POST requests
            self._method_not_allowed()

        def __getattr__(self, name: str):
            # BaseHTTPRequestHandler answers any method without a do_<METHOD> handler with 501, a 5xx.
            # Every method other than GET/HEAD (PUT, DELETE, PATCH, OPTIONS, TRACE, arbitrary tokens)
            # is a documented 405 instead. Only do_* names are resolved here (P6.4 fuzz finding).
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)
        
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress logging to keep tests deterministic
            pass
    
    # Create and return the server
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    return server