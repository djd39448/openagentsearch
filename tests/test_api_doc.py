import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
import tempfile
import os

import pytest

from openagentsearch.api.server import create_server
from openagentsearch.api.doc import make_doc_route


def test_doc_endpoint_valid() -> None:
    """Test that GET /doc/<valid_sha256> returns 200 with correct document data."""
    # Create a temporary directory structure
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create extracted directory and file
        extracted_dir = root / "extracted"
        extracted_dir.mkdir()
        
        doc_sha256 = "a" * 64  # 64 lowercase hex characters
        extracted_file = extracted_dir / f"{doc_sha256}.json"
        
        # Create a valid extracted document
        extracted_content = {
            "url": "https://example.com/test",
            "title": "Test Document",
            "lang": "en",
            "text": "This is the content of the test document.",
            "extracted_at": 1234567890.0
        }
        
        with open(extracted_file, "w", encoding="utf-8") as f:
            json.dump(extracted_content, f)
        
        # Create provenance file with matching entry
        provenance_dir = root / "raw"
        provenance_dir.mkdir()
        provenance_file = provenance_dir / "provenance.jsonl"
        
        provenance_entry = {
            "url": "https://example.com/test",
            "fetched_at": 1234567800.0,
            "status": 200,
            "sha256": doc_sha256,
            "robots_allowed": True
        }
        
        with open(provenance_file, "w", encoding="utf-8") as f:
            json.dump(provenance_entry, f)
            f.write("\n")
        
        # Create server with doc route
        server = create_server(
            "127.0.0.1", 
            0, 
            prefix_routes={"/doc/": make_doc_route(root)}
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/doc/{doc_sha256}"
            
            with urllib.request.urlopen(url, timeout=1) as response:
                assert response.getcode() == 200
                assert response.headers["Content-Type"] == "application/json; charset=utf-8"
                
                data = response.read()
                result = json.loads(data)
                
                # Verify all required fields are present in the response
                assert result["doc_sha256"] == doc_sha256
                assert result["url"] == extracted_content["url"]
                assert result["title"] == extracted_content["title"]
                assert result["lang"] == extracted_content["lang"]
                assert result["text"] == extracted_content["text"]
                assert result["extracted_at"] == extracted_content["extracted_at"]
                
                # Verify provenance is correctly returned
                expected_provenance = {
                    "url": provenance_entry["url"],
                    "fetched_at": provenance_entry["fetched_at"],
                    "status": provenance_entry["status"],
                    "sha256": provenance_entry["sha256"],
                    "robots_allowed": provenance_entry["robots_allowed"]
                }
                assert result["provenance"] == expected_provenance
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_doc_endpoint_unknown_hash() -> None:
    """Test that GET /doc/<unknown_sha256> returns 404."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create server with doc route but no files
        server = create_server(
            "127.0.0.1", 
            0, 
            prefix_routes={"/doc/": make_doc_route(root)}
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/doc/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            
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


def test_doc_endpoint_invalid_id_formats() -> None:
    """Test that various invalid id formats return 400."""
    invalid_ids = [
        "short",                 # Too short
        "a" * 63,                # One character too short 
        "a" * 65,                # One character too long
        "A" * 64,                # Uppercase characters (should not be allowed)
        "gg" * 32,               # Non-hex characters
        "a" * 64 + "/",          # Added trailing slash
        "a" * 64 + "/extra",     # Extra path segment
        "../" + "a" * 61,        # Path traversal attempt of the right length
    ]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create server with doc route
        server = create_server(
            "127.0.0.1", 
            0, 
            prefix_routes={"/doc/": make_doc_route(root)}
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            
            for invalid_id in invalid_ids:
                url = f"http://127.0.0.1:{port}/doc/{invalid_id}"
                
                try:
                    urllib.request.urlopen(url, timeout=1)
                    assert False, f"Expected HTTPError for {invalid_id}"
                except urllib.error.HTTPError as e:
                    assert e.getcode() == 400
                    data = e.read()
                    assert data == b'{"error":"invalid_sha256"}'
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_doc_endpoint_no_provenance() -> None:
    """Test that GET /doc/<valid_sha256> returns 200 with null provenance when no matching provenance entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create extracted directory and file
        extracted_dir = root / "extracted"
        extracted_dir.mkdir()
        
        doc_sha256 = "a" * 64  # 64 lowercase hex characters
        extracted_file = extracted_dir / f"{doc_sha256}.json"
        
        # Create a valid extracted document
        extracted_content = {
            "url": "https://example.com/test",
            "title": "Test Document",
            "lang": "en",
            "text": "This is the content of the test document.",
            "extracted_at": 1234567890.0
        }
        
        with open(extracted_file, "w", encoding="utf-8") as f:
            json.dump(extracted_content, f)
        
        # Do NOT create provenance file
        
        # Create server with doc route
        server = create_server(
            "127.0.0.1", 
            0, 
            prefix_routes={"/doc/": make_doc_route(root)}
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/doc/{doc_sha256}"
            
            with urllib.request.urlopen(url, timeout=1) as response:
                assert response.getcode() == 200
                
                data = response.read()
                result = json.loads(data)
                
                # Verify provenance is None (not a 404)
                assert result["provenance"] is None
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_doc_endpoint_exact_route_wins() -> None:
    """Test that existing exact routes take precedence over prefix routes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create extracted directory and file
        extracted_dir = root / "extracted"
        extracted_dir.mkdir()
        
        doc_sha256 = "a" * 64  # 64 lowercase hex characters
        extracted_file = extracted_dir / f"{doc_sha256}.json"
        
        # Create a valid extracted document (this will not be used since we have an exact route)
        extracted_content = {
            "url": "https://example.com/test",
            "title": "Test Document",
            "lang": "en",
            "text": "This is the content of the test document.",
            "extracted_at": 1234567890.0
        }
        
        with open(extracted_file, "w", encoding="utf-8") as f:
            json.dump(extracted_content, f)
        
        # Create an exact route for /doc/ that returns a different response
        def mock_doc_route(query_dict: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
            return (200, {"message": "This is an exact match!"})
        
        # Create server with both exact route and prefix route
        routes = {
            "/doc/": mock_doc_route  # This should take precedence
        }
        
        server = create_server(
            "127.0.0.1", 
            0, 
            routes=routes,
            prefix_routes={"/doc/": make_doc_route(root)}  # This shouldn't be used for /doc/
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/doc/"
            
            with urllib.request.urlopen(url, timeout=1) as response:
                assert response.getcode() == 200
                
                data = response.read()
                result = json.loads(data)
                
                # Should return the exact route response (not from prefix route)
                assert result["message"] == "This is an exact match!"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_healthz_unchanged() -> None:
    """Ensure that /healthz still works correctly after adding prefix routes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        
        # Create server with doc route
        server = create_server(
            "127.0.0.1", 
            0, 
            prefix_routes={"/doc/": make_doc_route(root)}
        )
        
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            port = server.server_address[1]
            url = f"http://127.0.0.1:{port}/healthz"
            
            with urllib.request.urlopen(url, timeout=1) as response:
                assert response.getcode() == 200
                assert response.headers["Content-Type"] == "application/json; charset=utf-8"
                
                data = response.read()
                result = json.loads(data)
                assert result["status"] == "ok"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()