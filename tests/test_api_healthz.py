import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from openagentsearch.api.server import create_server


def test_healthz_get() -> None:
    """Test that GET /healthz returns 200 with correct response."""
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    try:
        # Wait for server to start
        time.sleep(0.1)
        
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/healthz"
        
        with urllib.request.urlopen(url, timeout=1) as response:
            assert response.getcode() == 200
            assert response.headers["Content-Type"] == "application/json; charset=utf-8"
            data = response.read()
            assert data == b'{"status":"ok"}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_unknown_path() -> None:
    """Test that unknown paths return 404."""
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    try:
        # Wait for server to start
        time.sleep(0.1)
        
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/nope"
        
        try:
            urllib.request.urlopen(url, timeout=1)
            assert False, "Expected HTTPError for 404"
        except urllib.error.HTTPError as e:
            assert e.getcode() == 404
            data = e.read()
            assert data == b'{"error":"not_found"}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_healthz_head() -> None:
    """Test that HEAD /healthz returns correct headers and status with no body."""
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    try:
        # Wait for server to start
        time.sleep(0.1)
        
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/healthz"
        
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=1) as response:
            assert response.getcode() == 200
            assert response.headers["Content-Type"] == "application/json; charset=utf-8"
            # HEAD should not have a body
            data = response.read()
            assert len(data) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_post_method() -> None:
    """Test that POST /healthz returns 405 with correct response."""
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    try:
        # Wait for server to start
        time.sleep(0.1)
        
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/healthz"
        
        req = urllib.request.Request(url, method="POST")
        try:
            urllib.request.urlopen(req, timeout=1)
            assert False, "Expected HTTPError for 405"
        except urllib.error.HTTPError as e:
            assert e.getcode() == 405
            assert e.headers["Allow"] == "GET, HEAD"
            data = e.read()
            assert data == b'{"error":"method_not_allowed"}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()