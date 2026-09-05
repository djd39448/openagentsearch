# Getting Started

This is the onboarding document for agents and developers who want to use or extend
OpenAgentSearch as it exists today. Everything below describes implemented behaviour; the
[ROADMAP](../ROADMAP.md) records the build plan and [CONTRIBUTING](../CONTRIBUTING.md) the
contribution model.

## Requirements

- Python 3.12 or newer.
- The core project is standard-library-first: no third-party runtime dependencies.
- A local Ollama server is optional. It is required only when you use `OllamaEmbedClient` as the
  embedder; every other component accepts any object with an `embed(text) -> list[float]` method.
- The tests and the integration examples use injected embeddings and need no Ollama and no
  network access.

There is no one-command installer. Run from a checkout with the `src` directory on `PYTHONPATH`,
or install the package from the checkout with your preferred tool.

## Mental model

The implemented chain is exactly:

caller-supplied HTML
-> `openagentsearch.pipeline.index.index_document()`
-> existing HTML extractor (`openagentsearch.extract.html.extract`)
-> deterministic chunker (`openagentsearch.chunk.chunker.chunk_text`)
-> injected `embed(text)`
-> `VectorStore` (`openagentsearch.vector.store`)
-> `cosine_search()` (`openagentsearch.vector.search`)
-> optional HTTP `/search`
-> `/doc/{sha256}` when the extracted/raw provenance store has also been populated.

The important current split:

- `index_document()` writes vector rows only.
- `/doc/{sha256}` reads ExtractStore-compatible extracted and provenance files.
- The offline integration tests deliberately populate both stores using the same document SHA.
- There is not yet one production crawler command that performs both persistence paths
  automatically.

## Minimal offline indexing example

A complete example with no network and no Ollama. It writes only vector rows, does not touch the
extracted/provenance store, and does not start HTTP.

```python
import tempfile
from pathlib import Path

from openagentsearch.pipeline.index import index_document
from openagentsearch.vector.search import cosine_search
from openagentsearch.vector.store import VectorStore


class KeywordEmbedder:
    """Toy two-dimensional embedder; a real deployment injects OllamaEmbedClient instead."""

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "robots" in text.lower() else [0.0, 1.0]


HTML = (
    "<html><body>"
    "<p>A robots.txt file tells crawlers which paths they may fetch.</p>"
    "<p>An unrelated closing paragraph about something else entirely.</p>"
    "</body></html>"
)

with tempfile.TemporaryDirectory() as tmp:
    store = VectorStore(Path(tmp) / "vectors.sqlite", dimension=2)
    try:
        embedder = KeywordEmbedder()
        report = index_document(
            HTML, "https://example.invalid/robots-guide",
            store=store, embedder=embedder, chunk_size=64, overlap=8,
        )
        hits = cosine_search(store, embedder.embed("robots"), k=report.chunk_count)
        print(report.chunk_count, "chunks indexed; best match:", hits[0])
    finally:
        store.close()
```

`report` is an `IndexReport` carrying the document SHA-256, the source URL, the extracted-text
SHA-256, chunk counts and the chunk ids (`"<doc_sha256>:<chunk_index>"`).

## Using local Ollama

`OllamaEmbedClient` (`openagentsearch.embed.ollama`) is the only bundled real embedder:

- default base URL `http://localhost:11434`;
- default model `nomic-embed-text`;
- only the local Ollama endpoint is used;
- a connection failure raises `OllamaConnectionError`;
- no remote-provider fallback exists.

Pass an instance wherever an embedder is injected. Installing and running Ollama itself is out of
scope for this document.

## HTTP API

There is currently no HTTP server CLI; the API server is a Python-library integration surface.
Construct it in code:

```python
import threading
from pathlib import Path

from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.vector.store import VectorStore

store = VectorStore(Path("vectors.sqlite"), dimension=2)   # use your embedder's dimension
embedder = KeywordEmbedder()                                # or OllamaEmbedClient()
server = create_server(
    "127.0.0.1", 0,
    routes={"/search": make_search_route(store, embedder, lambda doc_sha256: None)},
    prefix_routes={"/doc/": make_doc_route(Path("data"))},
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print("listening on port", server.server_address[1])
# ... when finished:
server.shutdown(); server.server_close(); thread.join(); store.close()
```

The caller is responsible for starting and stopping `serve_forever()`. `/healthz` is always
registered. The third argument to `make_search_route` resolves a document SHA-256 to a URL (or
`None`). See [docs/agent-api.md](agent-api.md) for the exact `/search` and `/doc/{sha256}` contract
rather than duplicating every response field here.

## MCP search wrapper

Portable command, given a running local HTTP server:

```
python -m openagentsearch.mcp.server --base-url http://127.0.0.1:PORT
```

Exactly what it is:

- a stdio JSON-RPC subset;
- supports `initialize`, `tools/list`, `tools/call`;
- exposes one tool, `search`;
- delegates to the supplied local HTTP `/search`;
- does not implement resources, prompts, batch requests, notifications, or the full MCP surface.

## Evaluation and benchmark

- `eval/questions.jsonl` is synthetic, frozen fixture data; it is not production relevance ground
  truth.
- `scripts.eval` (`run_eval`, `recall_at_k`) computes recall@k over that set.
- `scripts.bench` (`run_benchmark`) benchmarks the real local cosine-search stack with an injected
  embedder and store.
- Generated results files are outputs of those functions, not proof of production quality.

Neither script exposes a command-line entry point today; call the functions from Python.

## Contribution safety

- `run_python_in_sandbox()` (`openagentsearch.sandbox.runner`) is process-level Python isolation,
  not a security boundary.
- It restricts environment inheritance, filesystem access, sockets and child-process operations
  through the current implementation (a fresh interpreter, a temporary working directory, an
  environment built from scratch, and an audit hook).
- It does not claim protection from hostile native code.
- `python -m scripts.gate --contribution <path> --results <path>` records the sandbox status of
  one contribution as a JSONL line.
- `mergeable` in that record remains `false` because sign-off and merge authorization are not
  implemented in code.
- The PowerShell gate script preserves the lint/format/type/test commands but depends on the
  corresponding tooling being runnable in that environment.

## Known limitations

- No live crawl-to-index daemon: fetching, extraction persistence and vector indexing are separate
  building blocks that the caller composes.
- No production index and no hosted service.
- No HTTP server CLI.
- No automated PR intake, merge or sign-off workflow.
- The sandbox is process-level isolation only.
- Embeddings come from the injected object or local Ollama; there is no remote provider.
