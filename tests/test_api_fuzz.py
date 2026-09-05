"""P6.4: deterministic input-validation sweep of the public API.

Every malformed or unexpected request must be answered with a 2xx/4xx response. A 5xx status, a
dropped connection, an exception surfacing to the client, or a timeout is a failure. The corpus is
bounded and seeded (random.Random(39448)); this is a validation sweep, not an unbounded fuzzer.
"""

import http.client
import json
import random
import socket
import string
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.vector.store import VectorStore

SEED = 39448
KNOWN_SHA = "c3" * 32  # a valid, existing document
UNKNOWN_SHA = "d4" * 32  # valid syntax, no such document
TIMEOUT = 5.0


class StubEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


def _start(tmpdir: str) -> dict:
    """Seeded store, one known document with provenance, both routes, server in a daemon thread."""
    root = Path(tmpdir)
    store = VectorStore(root / "vectors.sqlite3", 2)
    store.add("chunk-1", KNOWN_SHA, [1.0, 0.0], "the only chunk")
    (root / "extracted").mkdir()
    doc = {"url": "https://example.test/page", "title": "T", "lang": "en", "text": "x", "extracted_at": 1.0}
    (root / "extracted" / f"{KNOWN_SHA}.json").write_text(json.dumps(doc), encoding="utf-8")
    (root / "raw").mkdir()
    prov = {"url": doc["url"], "fetched_at": 0.5, "status": 200, "sha256": KNOWN_SHA, "robots_allowed": True}
    (root / "raw" / "provenance.jsonl").write_text(json.dumps(prov) + "\n", encoding="utf-8")
    embedder = StubEmbedder()
    server = create_server(
        "127.0.0.1",
        0,
        routes={"/search": make_search_route(store, embedder, lambda sha: None)},
        prefix_routes={"/doc/": make_doc_route(root)},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return {"server": server, "thread": thread, "store": store, "embedder": embedder, "base": f"http://127.0.0.1:{server.server_address[1]}"}


def _stop(env: dict) -> None:
    env["server"].shutdown()
    env["server"].server_close()
    env["thread"].join()
    env["store"].close()


def _probe(base: str, target: str, method: str = "GET") -> tuple[str, object]:
    """Return ("http", status) | ("client-rejected", reason) | ("drop", reason) | ("timeout", reason)."""
    try:
        request = urllib.request.Request(base + target, method=method)
    except (ValueError, http.client.InvalidURL) as exc:
        return ("client-rejected", repr(exc))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
            return ("http", response.getcode())
    except urllib.error.HTTPError as exc:
        exc.read()
        return ("http", exc.code)
    except (TimeoutError, socket.timeout) as exc:
        return ("timeout", repr(exc))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return ("timeout", repr(exc))
        return ("drop", repr(exc))
    except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionError) as exc:
        return ("drop", repr(exc))
    except (ValueError, http.client.InvalidURL) as exc:
        return ("client-rejected", repr(exc))


def _sweep(base: str, targets: list[tuple[str, str]]) -> tuple[list[str], dict[str, tuple[str, object]]]:
    """Probe every (method, target); return (failures, results by target)."""
    failures: list[str] = []
    results: dict[str, tuple[str, object]] = {}
    for method, target in targets:
        kind, detail = _probe(base, target, method)
        results[f"{method} {target}"] = (kind, detail)
        if kind in ("drop", "timeout") or (kind == "http" and 500 <= int(detail) <= 599):
            failures.append(f"{method} {target[:120]!r} -> {kind} {detail}")
    return failures, results


def _q(value: str) -> str:
    return "/search?" + urllib.parse.urlencode({"q": value})


def test_search_malformed_q_sweep_never_5xx():
    rng = random.Random(SEED)
    control_like = "".join(chr(c) for c in range(1, 32)) + "\x7f" + string.punctuation
    values = ["", "   ", "\t\n", "a" * 513, "b" * 1024, "c" * 4096, "line\r\nbreak", "nul\x00byte", "tab\there"]
    values += ["".join(rng.choice(control_like + string.ascii_letters) for _ in range(rng.randint(1, 80))) for _ in range(30)]
    targets = [("GET", "/search")] + [("GET", _q(v)) for v in values] + [("GET", "/search?q=first&q=second")]
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "\n".join(failures)
            for key, (kind, detail) in results.items():
                assert kind == "http" and (detail == 200 or 400 <= int(detail) <= 499), (key, kind, detail)
            assert _probe(env["base"], _q("alpha")) == ("http", 200)
        finally:
            _stop(env)


def test_search_malformed_k_sweep_never_5xx():
    rng = random.Random(SEED)
    ascii_pool = string.ascii_letters + string.digits + string.punctuation + " "
    values = ["0", "51", "-1", "-0", "+7", "1.5", "1e3", " 7", "7 ", "", "１２", "1" * 1000, "abc", "1a", "a1", "0x10", "٣"]
    values += ["".join(rng.choice(ascii_pool) for _ in range(rng.randint(1, 12))) for _ in range(25)]
    targets = [("GET", "/search?" + urllib.parse.urlencode({"q": "alpha", "k": v})) for v in values]
    targets.append(("GET", "/search?q=alpha&k=1&k=2"))
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "\n".join(failures)
            for key, (kind, detail) in results.items():
                assert kind == "http" and (detail == 200 or 400 <= int(detail) <= 499), (key, kind, detail)
            for good in ("1", "10", "50"):
                assert _probe(env["base"], f"/search?q=alpha&k={good}") == ("http", 200), good
        finally:
            _stop(env)


def test_doc_identifier_sweep_never_5xx():
    remainders = ["", "abc", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "%2e%2e", "../../etc/passwd", "..%2f..%2f",
                  "%5c..%5c..", "a//b", KNOWN_SHA + "?x=1", KNOWN_SHA + "/", KNOWN_SHA + "/extra", KNOWN_SHA + "%00",
                  UNKNOWN_SHA, "e" * 4096, "%2f" * 200, "." * 300, "%" * 50]
    targets = [("GET", "/doc/" + r) for r in remainders]
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "\n".join(failures)
            assert _probe(env["base"], "/doc/" + KNOWN_SHA) == ("http", 200)
            assert _probe(env["base"], "/doc/" + UNKNOWN_SHA) == ("http", 404)
            client_rejected = [k for k, (kind, _) in results.items() if kind == "client-rejected"]
            served = {k: v for k, v in results.items() if v[0] == "http"}
            assert len(served) >= len(targets) - len(client_rejected)
            for key, (kind, detail) in served.items():
                if key.endswith("/doc/" + KNOWN_SHA + "?x=1") or key.endswith("/doc/" + KNOWN_SHA):
                    assert detail == 200, key
                elif key.endswith("/doc/" + UNKNOWN_SHA):
                    assert detail == 404, key
                else:
                    assert 400 <= int(detail) <= 499, (key, detail)
        finally:
            _stop(env)


def test_method_sweep_never_5xx():
    rng = random.Random(SEED)
    methods = ["HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"]
    methods += ["".join(rng.choice(string.ascii_uppercase) for _ in range(rng.randint(3, 8))) for _ in range(4)]
    paths = ["/healthz", "/search?q=x", "/doc/" + KNOWN_SHA]
    targets = [(m, p) for m in methods for p in paths]
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "\n".join(failures)
            for key, (kind, detail) in results.items():
                assert kind == "http", (key, kind, detail)
                if key.startswith("HEAD "):
                    assert detail in (200, 404), key
                else:
                    assert detail == 405, key  # every non-GET/HEAD method is rejected, never 501
            assert _probe(env["base"], "/healthz", "POST") == ("http", 405)
        finally:
            _stop(env)


def test_malformed_percent_and_query_sweep_never_5xx():
    raw_targets = ["/search?q=%", "/search?q=%2", "/search?q=%GG", "/search?q=%00", "/search?q=a%0D%0Ab",
                   "/search?q=a&&&&k=1", "/search?q=" + "%41" * 2000, "/search??q=x", "/search?q=x&=y&z=",
                   "/search?%71=alpha", "/search?q=%C3%28", "/search?q=%E2%80%8B", "/doc/%00", "/doc/" + "%25" * 70,
                   "/doc/%2e%2e%2f%2e%2e%2f", "/healthz?%", "/healthz?a=%ZZ", "/search?q=x#frag"]
    targets = [("GET", t) for t in raw_targets]
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "\n".join(failures)
            for key, (kind, detail) in results.items():
                assert kind in ("http", "client-rejected"), (key, kind, detail)
                if kind == "http":
                    assert detail == 200 or 400 <= int(detail) <= 499, (key, detail)
        finally:
            _stop(env)


def test_aggregate_deterministic_sweep_is_clean():
    rng = random.Random(SEED)
    alphabet = string.ascii_letters + string.digits + "%+-_./"
    targets = []
    for _ in range(100):
        prefix = rng.choice(["/search?q=", "/doc/"])
        fragment = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 600)))
        targets.append(("GET", prefix + fragment))
    assert len(targets) == 100
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _start(tmpdir)
        try:
            failures, results = _sweep(env["base"], targets)
            assert failures == [], "5xx/drop/timeout findings:\n" + "\n".join(failures)
            assert all(kind in ("http", "client-rejected") for kind, _ in results.values())
        finally:
            _stop(env)
