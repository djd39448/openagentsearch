"""The default Ollama transport must read the response inside a context manager (no socket used)."""

import urllib.request

from openagentsearch.embed.ollama import OllamaEmbedClient


class _FakeResponse:
    """Records the context-manager protocol the way an HTTPResponse would honour it."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.entered = False
        self.exited = False
        self.read_calls = 0

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    def read(self) -> bytes:
        self.read_calls += 1
        return self.payload


def test_default_transport_reads_within_context_manager_and_closes():
    fake = _FakeResponse(b'{"embedding": [0.5, 1.0]}')
    seen = []

    def fake_urlopen(request, *args, **kwargs):
        seen.append(request)
        return fake

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        client = OllamaEmbedClient()
        request = urllib.request.Request("http://localhost:11434/api/embeddings", data=b"{}", method="POST")
        result = client._default_transport(request)
    finally:
        urllib.request.urlopen = real_urlopen

    assert result == b'{"embedding": [0.5, 1.0]}'
    assert seen == [request]
    assert fake.entered and fake.exited and fake.read_calls == 1
