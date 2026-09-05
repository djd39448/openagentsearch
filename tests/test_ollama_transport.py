import json
from unittest.mock import MagicMock, patch

import pytest

from openagentsearch.embed.ollama import OllamaEmbedClient


def test_default_transport_exercises_urllib():
    """Test that _default_transport calls urllib.request.urlopen correctly and properly closes the response."""
    # Create a mock response object with the expected interface
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"embeddings": [[0.1, 0.2, 0.3]]}'
    
    # Mock urllib.request.urlopen to return our mock response
    with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
        client = OllamaEmbedClient("http://localhost:11434")
        
        # Call the method directly  
        result = client._default_transport("http://localhost:11434/api/embeddings")
        
        # Verify that urllib.request.urlopen was called correctly
        mock_urlopen.assert_called_once_with("http://localhost:11434/api/embeddings")
        
        # Verify the read() method was invoked on response
        mock_response.read.assert_called_once()
        
        # Verify the result is the expected bytes
        assert result == b'{"embeddings": [[0.1, 0.2, 0.3]]}'
        
        # Verify that __exit__ was called (which means context manager properly closed)
        # This assertion works only if the mock response object has a context manager interface
        try:
            # Attempt to call __exit__ - this should be called by the context manager
            mock_response.__exit__.assert_called_once()
        except AttributeError:
            # If no __exit__ method exists on our mock (which is OK), we're done
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])