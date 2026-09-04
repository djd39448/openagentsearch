import urllib.request
import urllib.error
import json
from typing import Callable, List


class OllamaConnectionError(RuntimeError):
    """Raised when a connection to the Ollama server cannot be established."""
    pass


class OllamaEmbedClient:
    """A client for embedding text using an Ollama server."""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "nomic-embed-text", transport: Callable[[urllib.request.Request], bytes] | None = None) -> None:
        self.base_url = base_url
        self.model = model
        self.transport = transport or self._default_transport

    def _default_transport(self, request: urllib.request.Request) -> bytes:
        """Default transport that uses urllib.request.urlopen."""
        return urllib.request.urlopen(request).read()

    def embed(self, text: str) -> List[float]:
        """Embed the given text using the Ollama server.
        
        Args:
            text: The text to embed
            
        Returns:
            A list of float values representing the embedding
            
        Raises:
            OllamaConnectionError: If there's a connection error
            ValueError: If the response is malformed
        """
        # Prepare the request data
        data = {
            "model": self.model,
            "prompt": text
        }
        json_data = json.dumps(data).encode('utf-8')
        
        # Create the request
        url = f"{self.base_url}/api/embeddings"
        request = urllib.request.Request(url, data=json_data)
        request.add_header('Content-Type', 'application/json')
        
        try:
            response_bytes = self.transport(request)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise OllamaConnectionError(f"Failed to connect to Ollama server at {self.base_url}: {exc}") from exc
            
        # Parse the response
        try:
            response_data = json.loads(response_bytes)
        except json.JSONDecodeError:
            raise ValueError(f"Malformed JSON response from Ollama server: {response_bytes}")
            
        # Validate the response structure
        if "embedding" not in response_data:
            raise ValueError(f"Missing 'embedding' key in response: {response_data}")
            
        embedding = response_data["embedding"]
        
        # Validate that embedding is a list
        if not isinstance(embedding, list):
            raise ValueError(f"Embedding should be a list, got {type(embedding)}")
            
        # Validate that all elements are numbers
        if len(embedding) == 0:
            raise ValueError("Empty embedding returned by Ollama server")
            
        # Convert each element to float
        try:
            result = [float(x) for x in embedding]
        except (ValueError, TypeError):
            raise ValueError(f"Embedding contains non-numeric values: {embedding}")
            
        return result