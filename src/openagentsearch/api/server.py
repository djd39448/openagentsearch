import http.server
import json
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler
from typing import Any

JSONRoute = Callable[[dict[str, list[str]]], tuple[int, dict[str, object]]]


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    routes: Mapping[str, JSONRoute] | None = None,
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
            
            # Look up route
            route_func = default_routes.get(path)
            
            if route_func is None:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "not_found"}, separators=(",", ":")).encode("utf-8"))
            else:
                status, data = route_func(query_dict)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(response_data)))
                self.end_headers()
                self.wfile.write(response_data)
        
        def do_HEAD(self) -> None:
            # Parse the path and query parameters
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            
            # Look up route
            route_func = default_routes.get(path)
            
            if route_func is None:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
            else:
                status, data = route_func(query_dict)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                response_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(response_data)))
                self.end_headers()
        
        def do_POST(self) -> None:
            # Reject POST requests
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "method_not_allowed"}, separators=(",", ":")).encode("utf-8"))
        
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress logging to keep tests deterministic
            pass
    
    # Create and return the server
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    return server