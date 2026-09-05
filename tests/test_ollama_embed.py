import json
import urllib.error

from openagentsearch.embed.ollama import OllamaConnectionError, OllamaEmbedClient


def _client_returning(payload: bytes) -> OllamaEmbedClient:
    """A client whose injected transport returns `payload` without touching the network."""
    return OllamaEmbedClient(transport=lambda request: payload)


def _expect_value_error(payload: bytes, fragment: str) -> None:
    try:
        _client_returning(payload).embed("hello")
        assert False, f"{payload!r} should have raised ValueError"
    except ValueError as exc:
        assert fragment in str(exc), (payload, str(exc))


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
    """A transport failure becomes OllamaConnectionError naming the base URL, with the cause chained."""

    def transport(request):
        raise urllib.error.URLError("refused")

    client = OllamaEmbedClient(transport=transport)
    try:
        client.embed("hello")
        assert False, "Expected OllamaConnectionError to be raised"
    except OllamaConnectionError as e:
        assert "http://localhost:11434" in str(e)
        assert isinstance(e.__cause__, urllib.error.URLError)


def test_malformed_responses():
    """Malformed JSON and a missing embedding key are ValueError, via injected transports."""
    _expect_value_error(b"not json", "Malformed JSON")
    _expect_value_error(b"{}", "Missing 'embedding'")
    _expect_value_error(b'{"vector": [1.0]}', "Missing 'embedding'")


def test_bad_embeddings():
    """Non-list, empty and non-numeric embeddings are ValueError, via injected transports."""
    _expect_value_error(b'{"embedding": "abc"}', "should be a list")
    _expect_value_error(b'{"embedding": []}', "Empty embedding")
    _expect_value_error(b'{"embedding": [1.0, "x"]}', "non-numeric")
    _expect_value_error(b'{"embedding": [1.0, null]}', "non-numeric")


def test_top_level_non_object_response():
    """A top-level JSON array or scalar is rejected before the embedding key is looked up."""
    _expect_value_error(b'["this", "is", "an", "array"]', "Invalid Ollama response: expected object")
    _expect_value_error(b"42", "Invalid Ollama response: expected object")


def test_boolean_embedding_rejection():
    """Boolean elements anywhere in the vector are rejected instead of becoming 0.0/1.0."""
    _expect_value_error(b'{"embedding": [true, false]}', "Invalid embedding: contains boolean values")
    _expect_value_error(b'{"embedding": [0.5, true]}', "Invalid embedding: contains boolean values")
