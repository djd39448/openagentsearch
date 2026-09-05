import json
import urllib.error
from unittest.mock import Mock, patch
import pytest
from openagentsearch.embed.ollama import OllamaConnectionError, OllamaEmbedClient


def test_success():
    """An injected transport sees exactly the specified request and its reply is parsed to floats."""
    seen = []

    def transport(request):
        seen.append(request)
        return b'{"embedding": [0.1, 2, -3.5]}'

    client = OllamaEmbedClient(transport=transport)

    result = client.embed("hello")

    # Exactly one request, with the shape the P3.2 spec fixes.
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url == "http://localhost:11434/api/embeddings"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {"model": "nomic-embed-text", "prompt": "hello"}

    # Every element comes back as a float, including the integer 2.
    assert result == [0.1, 2.0, -3.5]
    for x in result:
        assert isinstance(x, float)


def test_connection_failure():
    """Test handling of connection errors."""
    # Create a mock transport that raises an exception
    mock_transport = Mock()
    mock_transport.side_effect = urllib.error.URLError("refused")
    
    # Create the client with our mock transport
    client = OllamaEmbedClient(transport=mock_transport)
    
    # Call embed method and verify it raises the right exception
    try:
        client.embed("hello")
        assert False, "Expected OllamaConnectionError to be raised"
    except OllamaConnectionError as e:
        assert "http://localhost:11434" in str(e)
        assert e.__cause__ is not None
        assert isinstance(e.__cause__, urllib.error.URLError)


def test_top_level_non_object_response():
    """Test that a top-level non-dict JSON response raises ValueError."""
    def transport(request):
        return b'["this", "is", "an", "array"]'  # Invalid: not an object

    client = OllamaEmbedClient(transport=transport)
    
    try:
        client.embed("hello")
        assert False, "Expected ValueError for non-object response"
    except ValueError as e:
        assert "Invalid Ollama response: expected object" in str(e)


def test_boolean_embedding_rejection():
    """Test that boolean embedding elements raise ValueError."""
    def transport(request):
        return b'{"embedding": [true, false]}'  # Invalid: contains booleans

    client = OllamaEmbedClient(transport=transport)
    
    try:
        client.embed("hello")
        assert False, "Expected ValueError for boolean embedding"
    except ValueError as e:
        assert "Invalid embedding: contains boolean values" in str(e)


# Remove the old malformed_responses and bad_embeddings tests which were only patching urllib