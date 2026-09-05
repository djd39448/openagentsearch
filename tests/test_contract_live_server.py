import json
import tempfile
import pathlib
from typing import Any, Dict

import pytest

from openagentsearch.api.contract import AGENT_API_CONTRACT
from openagentsearch.api.server import create_server
from openagentsearch.api.search import make_search_route
from openagentsearch.api.doc import make_doc_route
from openagentsearch.vector.store import VectorStore


def test_contract_matches_live_endpoint_behavior():
    """Test that the API contract matches the actual live endpoint behavior."""
    
    # Create a temporary vector store with one chunk (dimension 2)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(pathlib.Path(tmpdir) / "test.db", dimension=2)
        
        # Add one chunk to vector store (dimension 2)
        embedding = [0.5, 0.5]  # Fixed for testing
        store.add("chunk_1", "doc_1", embedding, "http://example.com")
        store.close()
        
        # Create a stub embedder
        def stub_embed(text: str) -> list[float]:
            return [0.5, 0.5]  # Return fixed vector
        
        # Create temporary extracted documents and provenance lines
        doc_root = pathlib.Path(tmpdir)
        doc_content = "This is the content of an extracted document."
        doc_sha = "a" * 64  # Valid 64-char lowercase hex
        
        # Write extracted document
        (doc_root / f"{doc_sha}.html").write_text(doc_content)
        
        # Write provenance line for the valid document
        provenance_file = doc_root / "provenance.jsonl"
        provenance_data = {
            "url": "http://example.com",
            "fetched_at": 1234567890.0,
            "status": 200,
            "sha256": doc_sha,
            "robots_allowed": True
        }
        with open(provenance_file, 'w') as f:
            f.write(json.dumps(provenance_data) + '\n')
        
        # Add second document without matching provenance
        doc_sha2 = "b" * 64
        (doc_root / f"{doc_sha2}.html").write_text("Content of second document")
        
        # Create routes
        search_route = make_search_route(store, stub_embed, lambda x: None)
        doc_route = make_doc_route(doc_root)
        
        # Start server on localhost with random port
        server = create_server(
            routes={"search": search_route},
            prefix_routes={"/doc/": doc_route}
        )
        server_port = server.server_address[1]
        
        try:
            import urllib.request
            
            # Test /search endpoint
            with urllib.request.urlopen(f"http://localhost:{server_port}/search?q=test") as f:
                response_data = json.loads(f.read().decode(), object_pairs_hook=list)
            
            # Check that top-level keys match
            expected_keys = AGENT_API_CONTRACT["GET /search"]["success"]["keys"]
            assert list(response_data.keys()) == expected_keys
            
            # Check that result objects have the correct keys (in order)
            results = response_data["results"]
            if results:  # Only if there are results
                result_obj = results[0]
                expected_result_keys = AGENT_API_CONTRACT["GET /search"]["success"]["nested"]["results"]["keys"]
                assert list(result_obj.keys()) == expected_result_keys
                
                # Check doc_url is null for the resolver that returns None
                assert result_obj["doc_url"] is None
            
            # Test /doc/{doc_sha256} endpoint with valid hash
            with urllib.request.urlopen(f"http://localhost:{server_port}/doc/{doc_sha}") as f:
                response_data = json.loads(f.read().decode(), object_pairs_hook=list)
            
            # Check that top-level keys match
            expected_keys = AGENT_API_CONTRACT["GET /doc/{doc_sha256}"]["success"]["keys"]
            assert list(response_data.keys()) == expected_keys
            
            # Check that provenance has the correct keys (in order)
            provenance = response_data["provenance"]
            if provenance is not None:  # If provenance is not null
                expected_provenance_keys = AGENT_API_CONTRACT["GET /doc/{doc_sha256}"]["success"]["nested"]["provenance"]["keys"]
                assert list(provenance.keys()) == expected_provenance_keys
            
            # Test /doc/{doc_sha256} endpoint with invalid hash (no provenance)
            with urllib.request.urlopen(f"http://localhost:{server_port}/doc/{doc_sha2}") as f:
                response_data = json.loads(f.read().decode(), object_pairs_hook=list)
            
            # Check that provenance is null for document without provenance
            assert response_data["provenance"] is None
            
        finally:
            server.shutdown()
            server.server_close()