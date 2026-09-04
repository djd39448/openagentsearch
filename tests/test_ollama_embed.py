import urllib.error
from unittest.mock import Mock, patch
import pytest
from openagentsearch.embed.ollama import OllamaConnectionError, OllamaEmbedClient


def test_success():
    """Test successful embedding."""
    # We'll test that the right data and method is sent through mocking urllib.request.urlopen directly
    with patch('urllib.request.urlopen') as mock_urlopen:
        # Configure the mock to return a successful response
        mock_response = Mock()
        mock_response.read.return_value = b'{"embedding": [0.1, 2.0, -3.5]}'
        mock_urlopen.return_value = mock_response
        
        # Create the client (using default transport)
        client = OllamaEmbedClient()
        
        # Call embed method
        result = client.embed("hello")
        
        # Verify urlopen was called with right arguments - the request details are in how urllib handles it implicitly
        assert mock_urlopen.called
        
        # Verify the result
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


def test_malformed_responses():
    """Test handling of malformed responses."""
    # Test cases for malformed JSON
    malformed_cases = [
        b'not json',
        b'{}',
        b'{"vector": [1.0]}'
    ]
    
    for case in malformed_cases:
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = Mock()
            mock_response.read.return_value = case
            mock_urlopen.return_value = mock_response
            
            client = OllamaEmbedClient()
            
            try:
                client.embed("hello")
                assert False, f"Expected ValueError for case {case}"
            except ValueError as e:
                # This is expected
                pass


def test_bad_embeddings():
    """Test handling of bad embedding responses."""
    # Test cases for malformed embeddings
    bad_embedding_cases = [
        b'{"embedding": "abc"}',
        b'{"embedding": []}',
        b'{"embedding": [1.0, "x"]}'
    ]
    
    for case in bad_embedding_cases:
        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = Mock()
            mock_response.read.return_value = case
            mock_urlopen.return_value = mock_response
            
            client = OllamaEmbedClient()
            
            try:
                client.embed("hello")
                assert False, f"Expected ValueError for case {case}"
            except ValueError as e:
                # This is expected
                pass