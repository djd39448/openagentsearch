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

- `index_document()` writes vector rows only, and writes them atomically: all chunks are embedded
  first, then stored in one transaction, so a failure part-way leaves no rows for that document.
  Indexing a document whose chunk ids already exist raises `ValueError` before any embedding.
- `/doc/{sha256}` reads ExtractStore-compatible extracted and provenance files.
- The offline integration tests deliberately populate both stores using the same document SHA.
- `openagentsearch.pipeline.ingest.LiveIngester.ingest(url)` performs both persistence paths for one
  allowlisted URL (raw bytes + provenance line, extracted record, vector rows) after robots.txt,
  page-budget and rate-limit checks; it follows no links itself and never follows a redirect.
  `openagentsearch.pipeline.crawl` (see "Bounded crawl" below) is the link-following loop built on
  top of it; it too has only been exercised against local test servers, and there is still no
  production crawler command run against the live internet.

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

## Indexing FLOP sources

`openagentsearch.sources` adapters (room directory, pinned-commit GitHub markdown, GitHub issues,
explicit site pages) all yield `SourceDoc`s for `index_source_document()` /
`index_source_documents()` (`openagentsearch.pipeline.index`), the content-type-aware sibling of
`index_document()`. A complete, no-network example using the room directory adapter:

```python
from pathlib import Path

from openagentsearch.pipeline.index import index_source_documents
from openagentsearch.sources.technocore_rooms import RoomDirectoryAdapter
from openagentsearch.vector.store import VectorStore


class KeywordEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "busy" in text.lower() else [0.0, 1.0]


store = VectorStore(Path("vectors.sqlite"), dimension=2)
adapter = RoomDirectoryAdapter(Path("rooms.jsonl"), min_messages=1)
report = index_source_documents(
    adapter.iter_documents(), store=store, embedder=KeywordEmbedder(),
    chunk_size=256, overlap=32,
)
print(report.indexed, "rooms indexed;", adapter.stats())
store.close()
```

`index_source_documents()` is sequential like `index_documents()` but does NOT stop at the first
failure (`SourceIndexReport.failed`/`.failures` record what went wrong per document); pass
`root=Path("data")` to also persist raw bytes and an extracted-document record so `/doc/{sha256}`
can serve the result. `VectorStore.manifest_kind_counts()` and the store-aware `/healthz`'s
`"kinds"` key report manifest counts broken out per adapter `kind` (`"room"`, `"github_doc"`,
`"github_issue"`, `"site"`).

## Bounded crawl

`openagentsearch.pipeline.crawl` (`python -m openagentsearch.pipeline.crawl`) is a config-driven,
bounded breadth-first crawl loop built on top of `LiveIngester`: it runs the four source adapters
above once, then follows links starting from a config's seed URLs, but ONLY inside hosts that
config allowlists and, where a host declares `path_prefixes`, only inside those paths. Politeness
(robots.txt, per-host rate limiting, bounded GET, no redirects) is entirely `LiveIngester`'s job;
this loop decides what to fetch next and when to stop.

```
python -m openagentsearch.pipeline.crawl --allowlist config/flop.yaml --root ./crawl-data \
    --db path/to/vectors.sqlite --embedder keyword
```

Required flags: `--allowlist PATH` (also accepted as `--config PATH`; the crawl config YAML, see
`config/flop.yaml`), `--root DIR`, `--db PATH`, `--embedder {ollama,keyword}`. The live FLOP run
against `config/flop.yaml`'s real hosts requires the operator's explicit go, per host, before every
run -- committing that file does not authorize running it.

Each host has a page budget (`hosts.<host>.max_pages` in the config, or `--max-pages-per-host N` to
override every host's budget for this invocation); the first URL that would exceed it is refused
and a `STOP-<host>` marker file is written under `--root` -- `openagentsearch.fetch.budget.PageBudget`,
unchanged from package A0/A1. The loop's own frontier/visited/per-host-budget-progress state is
checkpointed atomically (write-to-temp + `os.replace`) to `<root>/crawl-state.json` every
`--checkpoint-every` attempted pages (default 25) and once more on exit, success or exception.

`--resume` continues a previous run from that state file: it requires the file to exist (exit 2,
with a JSON error line on stderr, otherwise), refuses to resume against a config that has changed
since the state was saved (a `config_sha256` mismatch -- also exit 2; this check is NOT affected by
`--max-pages-per-host`, so raising a host's cap on a resumed run is exactly what that flag is for),
and reconstructs each host's remaining budget as `max_pages - <pages already counted against that
host's budget>`; nothing already visited is re-fetched. `CrawlReport` (the one compact JSON line
printed to stdout, and written to `<root>/crawl-report.json`) reports the crawl's cumulative
progress as of this invocation -- `pages_attempted`, `outcomes`, `indexed` and `hosts_stopped` carry
forward across resumed runs the same way the persisted state does.

Other flags: `--skip-sources` (crawl stage only), `--rooms-jsonl PATH` (overrides the config's
`sources.rooms_jsonl`), `--gh-runner-disabled` (skip the GitHub issues source instead of shelling
out to `gh`), `--seed URL` (repeatable; adds seeds beyond the config's own for a fresh run only --
`--resume` seeds its frontier from `crawl-state.json`, so `--seed` is silently ignored, and kept out
of the `config_sha256` fingerprint, on a `--resume` invocation), `--min-interval SECONDS`
(default `1.0`, per-host rate limit), `--dimension N`. Exit codes: `0` on a normal stop (frontier
exhausted or every host's budget exhausted), `2` for a bad `--resume` precondition (see above), `1`
for any other failure during the crawl (for example an embedder outage) -- either way, one JSON
`{"error": "..."}` line goes to stderr.

## HTTP API

### Server CLI

```
python -m openagentsearch.api.server --db path/to/vectors.sqlite --embedder keyword --host 127.0.0.1 --port 8080
```

Required flags: `--db PATH` (the SQLite file backing the `VectorStore`; its parent directory must
already exist) and `--embedder {ollama,keyword}`. Optional flags: `--host` (default
`127.0.0.1`), `--port` (default `8080`; `0` picks an ephemeral port), `--root DIR` (mounts
`/doc/{sha256}` from that directory when given; otherwise `/doc/*` is simply not registered),
`--dimension` (default `256` for `keyword`, `768` for `ollama`), and `--ollama-url` /
`--ollama-model` (only used with `--embedder ollama`). `--embedder keyword` uses
`KeywordEmbedder` (`openagentsearch.embed.keyword`) — a deterministic hashed-keyword embedder
for tests and demos, not a semantic one; see its docstring for exactly what it does not
guarantee. Constructing `--embedder ollama` never opens a connection by itself; a connection is
only attempted lazily, on the first `/search` request.

On success the process prints exactly one compact JSON line to stdout and flushes it, for example:

```
{"listening":"http://127.0.0.1:8080","db":"path/to/vectors.sqlite","embedder":"keyword","doc_route":false}
```

`doc_route` is `true` only when `--root` was given. The process then blocks until it receives
SIGINT, SIGTERM, or — on Windows — SIGBREAK (delivered as CTRL_BREAK_EVENT to a process launched
in its own process group). On that signal it shuts the server down, closes the store, prints
`{"stopped":true}`, and exits `0`. A failure before the server starts listening (for example a
`--db` parent directory that does not exist) prints one JSON `{"error": "..."}` line to stderr
instead and exits `1`; an invalid argument (an unknown `--embedder` choice, an out-of-range
`--port`, a `--root` that is not an existing directory, ...) is reported by argparse on stderr and
exits `2` before anything is built. Binding to a host other than `127.0.0.1` / `localhost` is
allowed but prints a `{"warning":"non-loopback host"}` line to stderr first — this CLI adds no
authentication or TLS of its own, so exposing it beyond localhost is the caller's responsibility.
(`--host ::1` is not treated as loopback: the server is built on a stdlib IPv4-only
`ThreadingHTTPServer`, so an IPv6 literal fails to bind at all rather than serving over IPv6.)

### Library integration

The API server is also a Python-library integration surface; the CLI above is a thin wrapper
around exactly this. Construct it in code:

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

A store-aware `/healthz` is available but not registered by default: pass
`routes={"/healthz": make_healthz_route(store), ...}` (`openagentsearch.api.healthz`) to
`create_server()` and the route reports `{"status": "ok", "index": {"indexed": N, "failed": N,
"superseded": N, "refused": N}}`, the same counts shape as `VectorStore.manifest_counts()`.

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

- No scheduler/daemon: `openagentsearch.pipeline.crawl` composes fetching, extraction persistence
  and vector indexing into one bounded, resumable run, but it is a one-shot process the operator
  starts (and, on `--resume`, restarts) by hand -- nothing here schedules or supervises repeated
  runs.
- No production index and no hosted service.
- `python -m openagentsearch.api.server` is a development/demo launcher, not a production process
  supervisor: no daemonization, no PID file, no log rotation, no automatic restart, and no
  authentication or TLS of its own.
- No automated PR intake, merge or sign-off workflow.
- The sandbox is process-level isolation only.
- Embeddings come from the injected object or local Ollama; there is no remote provider.
