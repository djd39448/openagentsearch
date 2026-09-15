"""Package A3 done-when: all four source adapters, through index_source_documents(), into one
VectorStore with a stub embedder and a temp root -- manifest_kind_counts() and /healthz report
counts by source kind, /doc/<sha256> serves a text-kind document with lang == "und" and provenance
status == 0, and one deliberately failing document is recorded as failed for its kind without
stopping the batch. No network anywhere: every adapter here is fed a fixture file or a fake
fetcher."""

import hashlib
import json
import tempfile
import threading
import urllib.request
from pathlib import Path


from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.healthz import make_healthz_route
from openagentsearch.api.server import create_server
from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.pipeline.index import index_source_documents
from openagentsearch.sources.base import SourceDoc
from openagentsearch.sources.flop_site import SitePagesAdapter
from openagentsearch.sources.github_docs import GitHubRepoDocsAdapter
from openagentsearch.sources.github_issues import GitHubIssuesAdapter
from openagentsearch.sources.technocore_rooms import RoomDirectoryAdapter
from openagentsearch.vector.store import VectorStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a3"
OWNER, REPO = "flop-contrib", "openagentsearch"
COMMIT = "b" * 40
FAIL_MARKER = "FAIL_MARKER_XYZ"


class _FakeFetcher:
    def __init__(self, responses: dict) -> None:
        self.responses = responses

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        return self.responses[url]


class _StubEmbedder:
    """Fixed 2-d embedder that raises for one marked chunk, to exercise the `failed` path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if FAIL_MARKER in text:
            raise RuntimeError("deliberate test failure")
        return [1.0, 0.0]


def _github_docs_adapter() -> GitHubRepoDocsAdapter:
    md_text = (FIXTURES / "github_repo_doc.md").read_text(encoding="utf-8")
    raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{COMMIT}/docs/example.md"
    response = FetchResponse(200, "text/plain", md_text.encode("utf-8"), False)
    fetcher = _FakeFetcher({raw_url: response})
    return GitHubRepoDocsAdapter(
        owner=OWNER, repo=REPO, commit=COMMIT, paths=["docs/example.md"], fetcher=fetcher,
    )


def _github_issues_adapter() -> GitHubIssuesAdapter:
    issue = {
        "number": 7,
        "title": "Integration test issue",
        "body": "Body of the integration-test issue.",
        "state": "open",
        "user": {"login": "alice"},
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "html_url": "https://github.com/flop-contrib/openagentsearch/issues/7",
        "comments": 0,
    }
    return GitHubIssuesAdapter(
        owner=OWNER, repo=REPO, issues=[issue], comments_by_number={}, fetched_at=1700300000.0,
    )


def _site_adapter() -> SitePagesAdapter:
    url = "https://flop.finance/overview"
    html = (
        "<html><head><title>Overview</title></head>"
        "<body><p>FLOP overview page.</p></body></html>"
    )
    response = FetchResponse(200, "text/html; charset=utf-8", html.encode("utf-8"), False)
    fetcher = _FakeFetcher({url: response})
    return SitePagesAdapter(urls=[url], fetcher=fetcher, allowed_hosts=["flop.finance"])


def _failing_doc() -> SourceDoc:
    return SourceDoc(
        url="https://flop.finance/will-fail",
        kind="site",
        content=f"{FAIL_MARKER} filler filler filler filler filler.",
        content_type="text",
        fetched_at=1700300000.0,
        provenance=(("reason", "deliberate-test-failure"),),
    )


def test_all_four_adapters_index_together_and_report_by_kind():
    room_adapter = RoomDirectoryAdapter(FIXTURES / "rooms_sample.jsonl", generated_at=1700200000.0)
    room_docs = list(room_adapter.iter_documents())
    github_doc_docs = list(_github_docs_adapter().iter_documents())
    github_issue_docs = list(_github_issues_adapter().iter_documents())
    site_docs = list(_site_adapter().iter_documents())
    failing_doc = _failing_doc()

    assert room_docs and github_doc_docs and github_issue_docs and site_docs

    all_docs = room_docs + github_doc_docs + github_issue_docs + site_docs + [failing_doc]

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "vectors.sqlite3", dimension=2)
        embedder = _StubEmbedder()
        try:
            report = index_source_documents(
                all_docs, store=store, embedder=embedder, chunk_size=2000, overlap=0, root=root,
            )

            # the failure does not stop the batch: everything else still gets attempted/indexed
            assert report.failed == 1
            assert report.indexed == len(all_docs) - 1
            assert len(report.failures) == 1
            assert report.failures[0][0] == failing_doc.url

            kind_counts = {kc.source_kind: kc.counts for kc in store.manifest_kind_counts()}
            for kind in ("room", "github_doc", "github_issue", "site"):
                assert kind in kind_counts, kind
                assert kind_counts[kind].indexed >= 1, kind
            assert kind_counts["site"].failed == 1

            # /healthz reports kinds with those four keys
            server = create_server(
                "127.0.0.1", 0,
                routes={"/healthz": make_healthz_route(store)},
                prefix_routes={"/doc/": make_doc_route(root)},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
                    body = json.loads(resp.read())
                assert set(body["kinds"].keys()) == {"room", "github_doc", "github_issue", "site"}
                assert body["kinds"]["room"]["indexed"] >= 1

                # /doc/<sha> of one text doc (a room doc) returns 200 with lang == "und" and
                # provenance status == 0 (the room adapter never puts a "status" in provenance).
                room_sha = hashlib.sha256(room_docs[0].content.encode("utf-8")).hexdigest()
                doc_url = f"http://127.0.0.1:{port}/doc/{room_sha}"
                with urllib.request.urlopen(doc_url, timeout=5) as resp:
                    assert resp.getcode() == 200
                    doc_body = json.loads(resp.read())
                assert doc_body["lang"] == "und"
                assert doc_body["provenance"] is not None
                assert doc_body["provenance"]["status"] == 0
                assert doc_body["url"] == room_docs[0].url
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
        finally:
            store.close()
