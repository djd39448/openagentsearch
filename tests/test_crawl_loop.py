"""Package A4 done-when: the bounded, resumable crawl loop.

Point 3 (real local server BFS + resume) runs a real `http.server` on 127.0.0.1:0 in a thread;
every other scenario uses an injected fetcher/gh-runner so no socket is opened at all. Every
server and every subprocess is torn down in a `finally` block so a bug here cannot hang the suite.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from openagentsearch.embed.keyword import KeywordEmbedder
from openagentsearch.pipeline.crawl import (
    CrawlResumeError,
    CrawlState,
    main as crawl_main,
    run_crawl,
)
from openagentsearch.pipeline.crawlconfig import (
    CrawlConfig,
    GitHubDocsSource,
    GitHubIssuesSource,
    HostRule,
)
from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.vector.store import VectorStore

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "a3"
ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"

PAGE1 = '<html><body><a href="/p2.html">two</a><a href="/p3.html">three</a></body></html>'
PAGE2 = '<html><body><a href="/p4.html">four</a></body></html>'
PAGE3 = '<html><body><a href="/p5.html">five</a></body></html>'
PAGE4 = "<html><body>page four, no outgoing links.</body></html>"
PAGE5 = "<html><body>page five, no outgoing links.</body></html>"


# --------------------------------------------------------------------------------------- fixtures


class _CountingHandler(BaseHTTPRequestHandler):
    """Serves `routes` (path -> body str) plus `/robots.txt`; logs every requested path onto the
    class itself so tests can assert exactly which URLs were (and were not) fetched."""

    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        type(self).seen.append(self.path)
        if self.path == "/robots.txt":
            body = ROBOTS_ALLOW_ALL.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        entry = self.routes.get(self.path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        body = entry.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self, routes: dict) -> None:
        _CountingHandler.routes = routes
        _CountingHandler.seen = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    @property
    def seen(self) -> list:
        return _CountingHandler.seen

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _five_page_server() -> _Server:
    return _Server({
        "/p1.html": PAGE1, "/p2.html": PAGE2, "/p3.html": PAGE3,
        "/p4.html": PAGE4, "/p5.html": PAGE5,
    })


def _store(root: Path, dimension: int = 64) -> VectorStore:
    return VectorStore(root / "v.sqlite", dimension)


# ============================================================================ 1. real-server BFS


def test_bfs_crawl_budget_and_resume_over_a_real_local_server():
    server = _five_page_server()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CrawlConfig(
                hosts=(HostRule("127.0.0.1", 3),),
                seeds=(f"{server.base}/p1.html",),
                rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
            )

            # ---- run 1: budget of 3 stops the crawl after exactly 3 real page fetches ----
            store = _store(root)
            try:
                report1 = run_crawl(
                    config, root=root, store=store, embedder=KeywordEmbedder(64),
                    chunk_size=64, overlap=8, resume=False, checkpoint_every=25,
                    min_interval_s=0.0, skip_sources=True,
                )
            finally:
                store.close()

            assert report1.stopped_reason == "budget"
            assert report1.hosts_stopped == ("127.0.0.1",)
            assert server.seen.count("/robots.txt") == 1
            assert server.seen.count("/p1.html") == 1
            assert server.seen.count("/p2.html") == 1
            assert server.seen.count("/p3.html") == 1
            assert "/p4.html" not in server.seen  # refused by budget before any GET
            assert "/p5.html" not in server.seen
            assert len(server.seen) == 4  # robots + 3 pages, nothing more

            assert (root / "STOP-127.0.0.1").exists()

            state1 = CrawlState.from_json(
                json.loads((root / "crawl-state.json").read_text(encoding="utf-8"))
            )
            assert len(state1.visited) == 3
            assert len(state1.frontier) > 0

            store = _store(root)
            try:
                assert store.manifest_counts().indexed == 3
            finally:
                store.close()

            # ---- run 2: --resume with a raised per-host cap fetches exactly the 2 remaining ----
            store = _store(root)
            try:
                report2 = run_crawl(
                    config, root=root, store=store, embedder=KeywordEmbedder(64),
                    chunk_size=64, overlap=8, resume=True, checkpoint_every=25,
                    min_interval_s=0.0, skip_sources=True, max_pages_override=5,
                )
            finally:
                store.close()

            assert report2.stopped_reason == "frontier_empty"
            assert server.seen.count("/p4.html") == 1  # no re-fetch of the first 3
            assert server.seen.count("/p5.html") == 1
            assert server.seen.count("/p1.html") == 1  # still exactly once, ever
            assert not (root / "STOP-127.0.0.1").exists()  # removed: budget had room again

            # CrawlReport numbers are cumulative across resumed runs, same convention as the
            # persisted CrawlState -- report2.indexed is the total indexed so far, not just the
            # 2 pages this particular process fetched.
            assert report2.indexed == 5
            assert dict(report2.outcomes)["refused_budget"] == 1  # still just the one, from run 1

            store = _store(root)
            try:
                assert store.manifest_counts().indexed == 5
            finally:
                store.close()

            state2 = CrawlState.from_json(
                json.loads((root / "crawl-state.json").read_text(encoding="utf-8"))
            )
            assert len(state2.visited) == 5
            assert state2.frontier == ()

            requests_before_run3 = len(server.seen)

            # ---- run 3: --resume again with nothing left in the frontier fetches nothing ----
            store = _store(root)
            try:
                report3 = run_crawl(
                    config, root=root, store=store, embedder=KeywordEmbedder(64),
                    chunk_size=64, overlap=8, resume=True, checkpoint_every=25,
                    min_interval_s=0.0, skip_sources=True, max_pages_override=5,
                )
            finally:
                store.close()

            assert report3.stopped_reason == "frontier_empty"
            assert report3.pages_attempted == report2.pages_attempted  # unchanged: nothing new
            assert report3.indexed == 5
            assert len(server.seen) == requests_before_run3  # zero new requests
    finally:
        server.stop()


def test_github_tree_fetch_asks_for_json_only_on_the_default_fetcher(monkeypatch):
    """api.github.com answers 415 to the fetcher's default `Accept: text/html`; the tree listing
    must be requested with the GitHub JSON media type when the real urllib fetcher is in use,
    while an injected fetcher keeps the plain four-argument Fetcher shape."""
    from openagentsearch.pipeline import crawl as crawl_module
    from openagentsearch.pipeline.ingest import FetchResponse

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_urllib_fetch(*args: object, **kwargs: object) -> FetchResponse:
        calls.append((args, kwargs))
        return FetchResponse(200, "application/json", b"{}", False)

    monkeypatch.setattr(crawl_module, "urllib_fetch", fake_urllib_fetch)
    crawl_module._fetch_github_tree(fake_urllib_fetch, "https://api.github.com/x")
    assert calls[-1][1] == {"accept": "application/vnd.github+json"}

    plain_calls: list[tuple[object, ...]] = []

    def injected(url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
        plain_calls.append((url, timeout_s, max_bytes, user_agent))
        return FetchResponse(200, "application/json", b"{}", False)

    crawl_module._fetch_github_tree(injected, "https://api.github.com/y")
    assert plain_calls == [
        ("https://api.github.com/y", 10.0, 5_000_000, "OpenAgentSearch-crawler/1.0")
    ]


def test_resume_without_state_file_is_refused():
    server = _five_page_server()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CrawlConfig(
                hosts=(HostRule("127.0.0.1", 3),), seeds=(f"{server.base}/p1.html",),
                rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
            )
            store = _store(root)
            try:
                with pytest.raises(CrawlResumeError):
                    run_crawl(
                        config, root=root, store=store, embedder=KeywordEmbedder(64),
                        chunk_size=64, overlap=8, resume=True, skip_sources=True,
                    )
            finally:
                store.close()
            assert server.seen == []  # nothing was ever attempted
    finally:
        server.stop()


class _TwoHostFetcher:
    """host-a has a single seed page with no outgoing links; host-b's one seed page links to two
    host-a pages. Never opens a socket."""

    pages = {
        "https://a.example/x": "<html><body>no links here.</body></html>",
        "https://a.example/y": "<html><body>no links here.</body></html>",
        "https://a.example/robots.txt": ROBOTS_ALLOW_ALL,
        "https://b.example/1": (
            '<html><body><a href="https://a.example/x">x</a>'
            '<a href="https://a.example/y">y</a></body></html>'
        ),
        "https://b.example/robots.txt": ROBOTS_ALLOW_ALL,
    }

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        content_type = "text/plain" if url.endswith("robots.txt") else "text/html; charset=utf-8"
        return FetchResponse(200, content_type, self.pages[url].encode("utf-8"), False)


def test_one_host_exhausted_while_another_host_stays_active_does_not_hang(tmp_path):
    """Regression test for a blocker: once host-a's budget (max_pages=1) is exhausted, the loop
    used to spin forever re-queuing host-a's leftover frontier URL, because host-b (max_pages=3,
    only 1 of 3 used) never reaches `budget.stopped()` on its own -- it simply has no more links
    left to discover, so the old `all(budget.stopped(h) for h in ingester.hosts)` check never
    became true and the frontier never emptied either. `run_crawl()` runs on a background daemon
    thread with a bounded join so a regression here fails this test instead of hanging the whole
    suite."""
    root = tmp_path / "out"
    root.mkdir()
    config = CrawlConfig(
        hosts=(HostRule("a.example", 1), HostRule("b.example", 3)),
        seeds=("https://b.example/1",),
        rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
    )
    store = _store(root)
    outcome: dict = {}

    def _run() -> None:
        try:
            outcome["report"] = run_crawl(
                config, root=root, store=store, embedder=KeywordEmbedder(64),
                chunk_size=64, overlap=8, resume=False, checkpoint_every=25,
                min_interval_s=0.0, skip_sources=True, fetcher=_TwoHostFetcher(),
            )
        except Exception as exc:  # pragma: no cover - surfaced via `outcome["error"]`
            outcome["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=10.0)
    try:
        assert not thread.is_alive(), "run_crawl() did not return -- multi-host budget hang"
        assert "error" not in outcome, outcome.get("error")
        report = outcome["report"]
        assert report.stopped_reason == "budget"
        assert report.hosts_stopped == ("a.example",)  # b.example never got stopped
        assert report.pages_attempted == 3
        assert dict(report.outcomes) == {"indexed": 2, "refused_budget": 1}
        state = CrawlState.from_json(
            json.loads((root / "crawl-state.json").read_text(encoding="utf-8"))
        )
        assert state.frontier == ("https://a.example/y",)  # left for a future --resume
    finally:
        store.close()


# ================================================================ 2. subprocess CLI, one full run


def _subprocess_env() -> dict:
    env = dict(os.environ)
    src_path = str(REPO / "src")
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src_path not in parts:
        parts.insert(0, src_path)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


class _SubprocessRunner:
    """Runs `python -m openagentsearch.pipeline.crawl ...` and captures stdout/stderr through a
    reader thread + bounded queue, so a stuck child fails the test with a timeout rather than
    hanging it. Always kill() in a finally block."""

    def __init__(self, args: list[str], timeout: float = 30.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openagentsearch.pipeline.crawl", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            cwd=str(REPO), env=_subprocess_env(),
        )
        self._out: "queue.Queue[str]" = queue.Queue()
        self._err: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._out), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self._err), daemon=True).start()
        try:
            self.exit_code = self.proc.wait(timeout=timeout)
        except Exception:
            # self.proc is already assigned at this point, so kill() can act on it even though
            # this exception (e.g. subprocess.TimeoutExpired) means
            # `runner = _SubprocessRunner(...)` at the call site never completes -- without this,
            # the call site's own try/finally (which calls .kill() on the bound `runner` name)
            # never runs either, and the child process is leaked for the rest of the
            # test-process lifetime.
            self.kill()
            raise
        self.stdout_lines = self._drain(self._out)
        self.stderr_lines = self._drain(self._err)

    @staticmethod
    def _pump(stream, q) -> None:
        for line in stream:
            q.put(line)

    @staticmethod
    def _drain(q) -> list[str]:
        lines = []
        try:
            while True:
                lines.append(q.get(timeout=1.0))
        except queue.Empty:
            pass
        return lines

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass


def test_bounded_crawl_via_subprocess_cli_reports_json_and_exits_zero():
    server = _five_page_server()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_doc = {
                "hosts": {"127.0.0.1": {"max_pages": 3}},
                "seeds": [f"{server.base}/p1.html"],
                "sources": {
                    "rooms_jsonl": None, "github_docs": [], "github_issues": [], "site_pages": [],
                },
            }
            config_path = root / "cfg.yaml"
            config_path.write_text(yaml.safe_dump(config_doc), encoding="utf-8")
            db_path = root / "v.sqlite"
            out_root = root / "out"

            runner = _SubprocessRunner([
                "--allowlist", str(config_path), "--root", str(out_root), "--db", str(db_path),
                "--embedder", "keyword", "--skip-sources", "--min-interval", "0",
            ])
            try:
                assert runner.exit_code == 0, "".join(runner.stderr_lines)
                assert len(runner.stdout_lines) == 1
                payload = json.loads(runner.stdout_lines[0])
                assert payload["stopped_reason"] == "budget"
                assert payload["pages_attempted"] >= 3
                assert payload["hosts_stopped"] == ["127.0.0.1"]
                assert payload["indexed"] == 3
                assert (out_root / "crawl-report.json").exists()
                on_disk = json.loads((out_root / "crawl-report.json").read_text(encoding="utf-8"))
                assert on_disk["stopped_reason"] == "budget"
            finally:
                runner.kill()
    finally:
        server.stop()


# ================================================================= 3. resume config-change exit 2


def test_resume_after_config_change_is_refused_with_exit_2(tmp_path, capsys):
    root = tmp_path / "out"
    db = tmp_path / "v.sqlite"
    config_doc = {
        "hosts": {"flop.finance": {"max_pages": 3}},
        "seeds": ["https://flop.finance/intro/"],
        "sources": {
            "rooms_jsonl": None, "github_docs": [], "github_issues": [], "site_pages": [],
        },
    }
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text(yaml.safe_dump(config_doc), encoding="utf-8")

    exit_code = crawl_main([
        "--allowlist", str(config_path), "--root", str(root), "--db", str(db),
        "--embedder", "keyword", "--skip-sources",
    ])
    assert exit_code == 0
    assert (root / "crawl-state.json").exists()

    # Change the config on disk (a different max_pages -- part of config_sha256) and resume.
    config_doc["hosts"]["flop.finance"]["max_pages"] = 9
    config_path.write_text(yaml.safe_dump(config_doc), encoding="utf-8")

    exit_code2 = crawl_main([
        "--allowlist", str(config_path), "--root", str(root), "--db", str(db),
        "--embedder", "keyword", "--skip-sources", "--resume",
    ])
    assert exit_code2 == 2
    captured = capsys.readouterr()
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert "config" in error["error"].lower()


def test_resume_with_extra_seed_is_not_refused_and_has_no_effect(tmp_path, capsys):
    """Regression test for a minor bug: `main()` used to fold `--seed` into `config.seeds` before
    computing `config_sha256` unconditionally -- even on `--resume`, where `run_crawl()` never
    reads `config.seeds` at all (the frontier is seeded from the persisted `crawl-state.json`
    frontier instead; see `run_crawl`'s `resume` branch). That made a `--resume` invocation with an
    added `--seed` look like a config change and raise `CrawlResumeError` (exit 2), even though the
    on-disk config file itself never changed and the extra seed could not have affected anything.
    Uses an empty `seeds` list so neither invocation ever needs to fetch anything real."""
    root = tmp_path / "out"
    db = tmp_path / "v.sqlite"
    config_doc = {
        "hosts": {"flop.finance": {"max_pages": 3}},
        "seeds": [],
        "sources": {
            "rooms_jsonl": None, "github_docs": [], "github_issues": [], "site_pages": [],
        },
    }
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text(yaml.safe_dump(config_doc), encoding="utf-8")

    exit_code = crawl_main([
        "--allowlist", str(config_path), "--root", str(root), "--db", str(db),
        "--embedder", "keyword", "--skip-sources",
    ])
    assert exit_code == 0
    capsys.readouterr()

    exit_code2 = crawl_main([
        "--allowlist", str(config_path), "--root", str(root), "--db", str(db),
        "--embedder", "keyword", "--skip-sources", "--resume",
        "--seed", "https://flop.finance/a-page-never-fetched/",
    ])
    captured = capsys.readouterr()
    assert exit_code2 == 0, captured.err
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(stdout_lines) == 1
    payload = json.loads(stdout_lines[0])
    assert payload["stopped_reason"] == "frontier_empty"
    assert payload["pages_attempted"] == 0  # the extra --seed was never fetched


def test_resume_without_state_file_via_main_exits_2_with_json_error(tmp_path, capsys):
    root = tmp_path / "out"
    db = tmp_path / "v.sqlite"
    config_doc = {
        "hosts": {"flop.finance": {"max_pages": 3}},
        "seeds": ["https://flop.finance/intro/"],
        "sources": {
            "rooms_jsonl": None, "github_docs": [], "github_issues": [], "site_pages": [],
        },
    }
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text(yaml.safe_dump(config_doc), encoding="utf-8")

    exit_code = crawl_main([
        "--allowlist", str(config_path), "--root", str(root), "--db", str(db),
        "--embedder", "keyword", "--skip-sources", "--resume",
    ])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert isinstance(error["error"], str) and error["error"]


# ===================================================================== 4. checkpoint-then-raise


class _ChainFetcher:
    """A fixed chain p1 -> p2 -> p3 served from memory; never opens a socket."""

    def __init__(self) -> None:
        self.pages = {
            "https://chain.test/p1.html": '<html><body><a href="/p2.html">2</a></body></html>',
            "https://chain.test/p2.html": '<html><body><a href="/p3.html">3</a></body></html>',
            "https://chain.test/p3.html": (
                "<html><body>THIRD_PAGE_MARKER content here.</body></html>"
            ),
            "https://chain.test/robots.txt": ROBOTS_ALLOW_ALL,
        }

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        content_type = "text/plain" if url.endswith("robots.txt") else "text/html; charset=utf-8"
        return FetchResponse(200, content_type, self.pages[url].encode("utf-8"), False)


class _FailOnMarkerEmbedder:
    """Raises on the one chunk whose text contains THIRD_PAGE_MARKER; otherwise a fixed vector."""

    def embed(self, text: str) -> list[float]:
        if "THIRD_PAGE_MARKER" in text:
            raise RuntimeError("deliberate embedder failure on the third page")
        return [1.0] + [0.0] * 63


def test_checkpoint_every_one_persists_state_before_an_ingest_failure_propagates(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    config = CrawlConfig(
        hosts=(HostRule("chain.test", 10),), seeds=("https://chain.test/p1.html",),
        rooms_jsonl=None, github_docs=(), github_issues=(), site_pages=(),
    )
    store = _store(root)
    try:
        with pytest.raises(RuntimeError, match="deliberate embedder failure"):
            run_crawl(
                config, root=root, store=store, embedder=_FailOnMarkerEmbedder(),
                chunk_size=64, overlap=8, resume=False, checkpoint_every=1,
                min_interval_s=0.0, skip_sources=True, fetcher=_ChainFetcher(),
            )
    finally:
        store.close()

    assert (root / "crawl-state.json").exists()
    state_text = (root / "crawl-state.json").read_text(encoding="utf-8")
    state = CrawlState.from_json(json.loads(state_text))
    assert state.pages_attempted == 2
    assert len(state.visited) == 2
    assert "https://chain.test/p3.html" not in state.visited


# ============================================================================ 5. sources stage


ROOM_A = {
    "id": "room-alpha", "classification_hint": "busy", "first_seen_ts": 1700000000,
    "last_activity_ts": 1700003600, "last_seq": 42, "message_count_seen": 12,
    "sample_from_dids": ["did:plc:aaa"],
}
ROOM_B = {
    "id": "room-beta", "classification_hint": "active", "first_seen_ts": 1700100000,
    "last_activity_ts": 1700103600, "last_seq": 7, "message_count_seen": 3,
    "sample_from_dids": [],
}

GH_OWNER, GH_REPO, GH_COMMIT = "flop-labs", "sourcetest", "c" * 40
SITE_URL = "https://flop.finance/overview"


class _SourcesFakeFetcher:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        return self.responses[url]


def _fake_gh_runner(issue: dict):
    def runner(argv: list[str]) -> bytes:
        joined = " ".join(argv)
        if "issues?state=all" in joined:
            return json.dumps([issue]).encode("utf-8")
        raise AssertionError(f"unexpected gh invocation for this test: {argv}")

    return runner


def test_sources_stage_four_adapters_yield_and_report_by_kind(tmp_path):
    rooms_path = tmp_path / "rooms.jsonl"
    rooms_path.write_text(
        json.dumps(ROOM_A) + "\n" + json.dumps(ROOM_B) + "\n", encoding="utf-8",
    )

    md_text = (FIXTURES / "github_repo_doc.md").read_text(encoding="utf-8")
    tree_url = (
        f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/git/trees/{GH_COMMIT}?recursive=1"
    )
    raw_url = f"https://raw.githubusercontent.com/{GH_OWNER}/{GH_REPO}/{GH_COMMIT}/docs/example.md"
    tree_json = {
        "sha": GH_COMMIT, "truncated": False,
        "tree": [{"path": "docs/example.md", "type": "blob"}],
    }
    site_html = (
        "<html><head><title>Overview</title></head><body><p>FLOP overview page.</p></body></html>"
    )

    fetch = _SourcesFakeFetcher({
        tree_url: FetchResponse(
            200, "application/json", json.dumps(tree_json).encode("utf-8"), False
        ),
        raw_url: FetchResponse(200, "text/plain", md_text.encode("utf-8"), False),
        SITE_URL: FetchResponse(200, "text/html; charset=utf-8", site_html.encode("utf-8"), False),
    })

    issue = {
        "number": 3, "title": "Sources stage test issue", "body": "body text",
        "state": "open", "user": {"login": "carol"}, "created_at": "2024-02-01T00:00:00Z",
        "updated_at": "2024-02-01T00:00:00Z",
        "html_url": f"https://github.com/{GH_OWNER}/{GH_REPO}/issues/3", "comments": 0,
    }

    config = CrawlConfig(
        hosts=(HostRule("flop.finance", 5),),  # so SitePagesAdapter accepts SITE_URL's host
        seeds=(),
        rooms_jsonl=None,
        github_docs=(GitHubDocsSource(GH_OWNER, GH_REPO, GH_COMMIT),),
        github_issues=(GitHubIssuesSource(GH_OWNER, GH_REPO),),
        site_pages=(SITE_URL,),
    )

    root = tmp_path / "out"
    db_path = tmp_path / "v.sqlite"
    store = VectorStore(db_path, 2)
    try:
        report = run_crawl(
            config, root=root, store=store,
            embedder=_TwoDimStubEmbedder(), chunk_size=2000, overlap=0,
            resume=False, checkpoint_every=25, min_interval_s=0.0, skip_sources=False,
            rooms_jsonl=str(rooms_path), gh_runner=_fake_gh_runner(issue), fetcher=fetch,
        )
    finally:
        store.close()

    assert report.sources is not None
    assert len(report.sources) == 4
    for name, stats in report.sources:
        assert stats.yielded >= 1, name

    verify_store = VectorStore(db_path, 2)
    try:
        kind_counts = {kc.source_kind: kc.counts for kc in verify_store.manifest_kind_counts()}
    finally:
        verify_store.close()
    assert set(kind_counts) == {"room", "github_doc", "github_issue", "site"}
    for kind in kind_counts:
        assert kind_counts[kind].indexed >= 1, kind


class _TwoDimStubEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]
