"""Command-line launcher for the OpenAgentSearch HTTP API server.

`python -m openagentsearch.api.server ...` (the module re-exports `main` from here) starts a
`ThreadingHTTPServer` wired to a `VectorStore` and an embedder, plus (package B2) an optional
`--ledger PATH` compact reputation ledger mounted at `/did/{did}` -- absent, `/did/{did}` answers
`ledger_not_built` for every well-formed DID, exactly like the public Worker before it is built
one. Prints one compact JSON line to stdout describing where it is listening, and then blocks
until it receives SIGINT, SIGTERM, or (on Windows) SIGBREAK / CTRL_BREAK_EVENT. On that signal it
shuts the server down, closes the store, prints one final JSON line, and exits 0.

This is a development/demo launcher, not a production process supervisor: there is no
daemonization, no PID file, no log rotation, and no automatic restart or health monitoring.
Binding to a non-loopback host is permitted but is flagged with a warning line on stderr before
the server starts serving - this module does not itself add authentication, TLS, or any other
protection for a non-loopback deployment; that remains the caller's responsibility.
"""

import argparse
import json
import signal
import sys
import threading
from collections.abc import Sequence
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import FrameType

from openagentsearch.api.did import make_did_prefix_route
from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.healthz import make_healthz_route
from openagentsearch.api.search import DocURLResolver, Embedder, make_search_route
from openagentsearch.api.server import JSONRoute, PrefixJSONRoute, create_server
from openagentsearch.embed.keyword import KeywordEmbedder
from openagentsearch.embed.ollama import OllamaEmbedClient
from openagentsearch.reputation.compact import CompactLedger, load_compact_ledger
from openagentsearch.vector.store import VectorStore

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
# "::1" (the IPv6 loopback literal) is deliberately excluded: create_server() builds a stdlib
# http.server.ThreadingHTTPServer, whose address_family is hardcoded to socket.AF_INET, so
# binding "::1" always raises (gaierror) before this warning check is ever reached - treating it
# as a recognized loopback host would silently misdocument a host that can never actually work.
_DEFAULT_DIMENSION = {"keyword": 256, "ollama": 768}


def _port(value: str) -> int:
    """argparse `type=`: an integer in `0..65535` inclusive; raises `ArgumentTypeError`
    (argparse turns this into an exit code 2) for anything else."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 0..65535, got {port}")
    return port


def _positive_int(value: str) -> int:
    """argparse `type=`: a positive integer; raises `ArgumentTypeError` otherwise."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {number}")
    return number


def _existing_directory(value: str) -> Path:
    """argparse `type=`: an existing directory path; raises `ArgumentTypeError` for a missing
    path or a path that exists but is not a directory (for example a plain file)."""
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not an existing directory: {value!r}")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.api.server")
    parser.add_argument("--db", required=True, help="SQLite file backing the VectorStore")
    parser.add_argument("--embedder", required=True, choices=("ollama", "keyword"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8080, help="0 picks an ephemeral port")
    parser.add_argument(
        "--root",
        type=_existing_directory,
        default=None,
        help="when given, mounts /doc/ from this directory",
    )
    parser.add_argument(
        "--dimension",
        type=_positive_int,
        default=None,
        help="default: 256 for keyword, 768 for ollama",
    )
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="nomic-embed-text")
    parser.add_argument(
        "--ledger",
        default=None,
        help="optional: compact reputation-ledger JSON file (mounts /did/{did}, package B2)",
    )
    return parser


def _resolve_doc_url_factory(store: VectorStore) -> DocURLResolver:
    """Build a resolver mapping a document SHA-256 to the `source_url` recorded for it in
    `store`'s index manifest, or `None` when that document has no manifest row (for example a
    vector-only store written before the manifest existed, or a chunk the manifest never
    recorded)."""

    def resolve(doc_sha256: str) -> str | None:
        entry = store.manifest_entry(doc_sha256)
        return entry.source_url if entry is not None else None

    return resolve


def build_server(args: argparse.Namespace) -> tuple[ThreadingHTTPServer, VectorStore]:
    """Construct the `VectorStore`, embedder, and `ThreadingHTTPServer` described by parsed CLI
    arguments (from `_build_parser().parse_args(...)`, with `args.dimension` already resolved to
    a concrete integer).

    Does not start serving and does not print anything: the caller runs `serve_forever()`
    (typically on a background thread) and is responsible for eventually calling `shutdown()`,
    `server_close()`, and `store.close()`. Constructing `OllamaEmbedClient` never opens a network
    connection - the connection, if any, happens lazily on the first `embed()` call. When
    `args.ledger` is given, it is loaded fail-closed (`compact.load_compact_ledger`) BEFORE the
    server is created, so a malformed or missing ledger file fails `build_server()` itself rather
    than being silently ignored.

    Raises whatever the store, embedder, ledger load, or socket bind raises (for example a `--db`
    path whose parent directory does not exist, or a `--port` already in use); `main()` turns that
    into the documented stderr/exit-code contract rather than letting it propagate. Any such
    failure that happens after the `VectorStore` is constructed closes that store before
    re-raising, so a partially-built server never leaks the store's open SQLite connection/file
    handle.
    """
    store = VectorStore(args.db, args.dimension)
    try:
        embedder: Embedder
        if args.embedder == "keyword":
            embedder = KeywordEmbedder(args.dimension)
        else:
            embedder = OllamaEmbedClient(args.ollama_url, args.ollama_model)
        ledger: CompactLedger | None = None
        if args.ledger is not None:
            ledger = load_compact_ledger(Path(args.ledger))
        resolve_doc_url = _resolve_doc_url_factory(store)
        routes: dict[str, JSONRoute] = {
            "/healthz": make_healthz_route(store, ledger),
            "/search": make_search_route(store, embedder, resolve_doc_url),
        }
        # `/did/` is always mounted (like the Worker's `makeWorker(index, ledger = null)`): a
        # well-formed DID answers `ledger_not_built` when `ledger is None`, exactly as documented,
        # rather than a bare 404 not_found.
        prefix_routes: dict[str, PrefixJSONRoute] = {"/did/": make_did_prefix_route(ledger)}
        if args.root is not None:
            prefix_routes["/doc/"] = make_doc_route(args.root)
        server = create_server(args.host, args.port, routes=routes, prefix_routes=prefix_routes)
    except Exception:
        store.close()
        raise
    return server, store


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, build and start the server, print the startup line, and block until a
    stop signal arrives.

    Returns 0 after a clean signal-triggered shutdown. Returns 1, after printing a one-line JSON
    `{"error": "..."}` to stderr, if the server could not be built (bad `--db` path, a store
    error, or a port already in use) - nothing is printed to stdout in that case, and no signal
    handlers are installed. Argument-parsing failures (an unknown `--embedder` choice, a `--port`
    or `--dimension` out of range, a `--root` that is not an existing directory, a missing
    required argument, ...) exit the process directly with status 2 via argparse's own behaviour
    and never reach this function's return statement.
    """
    args = _build_parser().parse_args(argv)
    if args.dimension is None:
        args.dimension = _DEFAULT_DIMENSION[args.embedder]

    try:
        server, store = build_server(args)
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
        return 1

    if args.host not in _LOOPBACK_HOSTS:
        warning = json.dumps({"warning": "non-loopback host"}, separators=(",", ":"))
        print(warning, file=sys.stderr, flush=True)

    stop_event = threading.Event()

    def _on_stop_signal(signum: int, frame: FrameType | None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _on_stop_signal)
    signal.signal(signal.SIGTERM, _on_stop_signal)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, _on_stop_signal)

    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    host, port = str(server.server_address[0]), int(server.server_address[1])
    listening = {
        "listening": f"http://{host}:{port}",
        "db": str(args.db),
        "embedder": args.embedder,
        "doc_route": args.root is not None,
    }
    print(json.dumps(listening, separators=(",", ":")), flush=True)

    # A short-timeout poll loop, not an unbounded stop_event.wait(): on Windows, a Python signal
    # handler only actually runs when the interpreter returns to the bytecode eval loop, which an
    # indefinite blocking wait never does until *something* wakes it - so an unbounded wait can
    # sit past a delivered SIGINT/SIGTERM/SIGBREAK indefinitely. Waking up every 0.1s is what
    # lets the handler (which only sets stop_event) actually get a chance to run promptly.
    while not stop_event.wait(timeout=0.1):
        pass
    server.shutdown()
    server.server_close()
    serve_thread.join(timeout=10)
    store.close()
    print(json.dumps({"stopped": True}, separators=(",", ":")), flush=True)
    return 0
