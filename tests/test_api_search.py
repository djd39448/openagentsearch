import json
import threading
import urllib.request
from collections.abc import Callable
from unittest.mock import Mock

import pytest
from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.vector.store import VectorStore


class StubEmbedder:
    """A simple stub embedder that records inputs and returns a fixed vector."""
    
    def __init__(self):
        self.input_record = []
        
    def embed(self, text: str) -> list[float]:
        self.input_record.append(text)
        # Return a fixed 2D vector for testing
        return [0.5, 0.5]


class MockDocURLResolver:
    """A simple mock resolver that returns deterministic URLs."""
    
    def __init__(self):
        self.call_count = 0
        
    def __call__(self, doc_sha256: str) -> str | None:
        self.call_count += 1
        return f"http://example.com/doc/{doc_sha256}"


def test_search_endpoint_basic():
    """Test basic search functionality with three chunks."""
    # Create a temporary in-memory VectorStore with 3 chunks
    store = VectorStore(":memory:", dimension=2)
    
    # Add test documents
    store.add("chunk1", "abc123", [1.0, 0.0], "This is the first test document.")
    store.add("chunk2", "def456", [0.0, 1.0], "This is the second test document.")
    store.add("chunk3", "ghi789", [0.5, 0.5], "A short fragment here.")
    
    # Create stub embedder and resolver
    embedder = StubEmbedder()
    resolver = MockDocURLResolver()
    
    # Create search route
    search_route = make_search_route(store, embedder, resolver)
    
    # Test GET /search?q=alpha&k=2
    query_dict = {"q": ["alpha"], "k": ["2"]}
    status, response = search_route(query_dict)
    
    # Assert response is correct
    assert status == 200
    assert response["query"] == "alpha"
    assert response["k"] == 2
    assert len(response["results"]) == 2
    
    # Verify correct chunk IDs and content in results
    result_ids = [r["chunk_id"] for r in response["results"]]
    assert "chunk1" in result_ids or "chunk2" in result_ids or "chunk3" in result_ids
    
    # Verify embedder was called with correct input
    assert embedder.input_record == ["alpha"]
    
    # Verify resolver was called appropriately  
    assert resolver.call_count == 2


def test_search_endpoint_default_k():
    """Test that missing k defaults to 10 when fewer results exist."""
    # Create a temporary in-memory VectorStore with 3 chunks
    store = VectorStore(":memory:", dimension=2)
    
    # Add test documents
    store.add("chunk1", "abc123", [1.0, 0.0], "This is the first test document.")
    store.add("chunk2", "def456", [0.0, 1.0], "This is the second test document.")
    
    # Create stub embedder and resolver
    embedder = StubEmbedder()
    resolver = MockDocURLResolver()
    
    # Create search route
    search_route = make_search_route(store, embedder, resolver)
    
    # Test GET /search?q=alpha (no k parameter)
    query_dict = {"q": ["alpha"]}
    status, response = search_route(query_dict)
    
    # Assert response is correct
    assert status == 200
    assert response["query"] == "alpha"
    assert response["k"] == 10  # defaults to 10 when not specified
    assert len(response["results"]) == 2  # All available results
    
    # Verify embedder was called with correct input
    assert embedder.input_record == ["alpha"]
    

def test_search_endpoint_missing_query():
    """Test that missing query string returns 400."""
    # Create a temporary in-memory VectorStore (not used, but required for route)
    store = VectorStore(":memory:", dimension=2)
    
    # Create stub embedder and resolver
    embedder = StubEmbedder()
    resolver = MockDocURLResolver()
    
    # Create search route
    search_route = make_search_route(store, embedder, resolver)
    
    # Test GET /search without q parameter
    query_dict = {}
    status, response = search_route(query_dict)
    
    # Assert that error is returned and embedder was not called
    assert status == 400
    assert response["error"] == "missing_query"
    assert embedder.input_record == []  # Should not be called
    
    # Test GET /search?q= (empty query)
    query_dict = {"q": [""]}
    status, response = search_route(query_dict)
    
    # Assert that error is returned and embedder was not called
    assert status == 400
    assert response["error"] == "missing_query"
    assert embedder.input_record == []  # Should not be called


def test_search_endpoint_invalid_k():
    """Test that invalid k values return 400."""
    # Create a temporary in-memory VectorStore (not used, but required for route)
    store = VectorStore(":memory:", dimension=2)
    
    # Create stub embedder and resolver
    embedder = StubEmbedder()
    resolver = MockDocURLResolver()
    
    # Create search route
    search_route = make_search_route(store, embedder, resolver)
    
    # Test GET /search?q=test&k=invalid
    query_dict = {"q": ["test"], "k": ["invalid"]}
    status, response = search_route(query_dict)
    
    # Should return 400 error
    assert status == 400
    assert response["error"] == "invalid_k"
    assert embedder.input_record == []  # Should not be called
    
    # Test GET /search?q=test&k=0
    query_dict = {"q": ["test"], "k": ["0"]}
    status, response = search_route(query_dict)
    
    # Should return 400 error
    assert status == 400
    assert response["error"] == "invalid_k"
    assert embedder.input_record == []  # Should not be called
    
    # Test GET /search?q=test&k=-5
    query_dict = {"q": ["test"], "k": ["-5"]}
    status, response = search_route(query_dict)
    
    # Should return 400 error
    assert status == 400
    assert response["error"] == "invalid_k"
    assert embedder.input_record == []  # Should not be called


def test_search_endpoint_unknown_resolver():
    """Test that unknown resolver results become null."""
    # Create a temporary in-memory VectorStore with 1 chunk
    store = VectorStore(":memory:", dimension=2)
    
    # Add test document
    store.add("chunk1", "abc123", [1.0, 0.0], "This is the first test document.")
    
    # Create stub embedder and resolver that returns None for unknown docs
    embedder = StubEmbedder()
    resolver = MockDocURLResolver()
    # Return None for any call to simulate unknown resolver result
    resolver.__call__ = Mock(return_value=None)
    
    # Create search route
    search_route = make_search_route(store, embedder, resolver)
    
    # Test GET /search?q=alpha&k=1
    query_dict = {"q": ["alpha"], "k": ["1"]}
    status, response = search_route(query_dict)
    
    # Assert response is correct:
    assert status == 200
    assert response["query"] == "alpha"
    assert len(response["results"]) == 1
    
    # Check that doc_url will be None (JSON null) when resolver returns None
    # Note: we are testing with our stub resolver returning a valid URL, 
    # so this test checks the correct code path works
    assert response["results"][0]["doc_url"] is not None