"""Package A3: SitePagesAdapter over an explicit URL list with an injected fetcher. No network."""

from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.sources.flop_site import SitePagesAdapter

ALLOWED_HOST = "flop.finance"
HTML_PAGE = (
    "<html><head><title>FLOP Yellow Paper</title></head>"
    "<body><p>The FLOP protocol yellow paper, section overview.</p></body></html>"
)


class _FakeFetcher:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        return self.responses[url]


def _html_response(body: str) -> FetchResponse:
    return FetchResponse(200, "text/html; charset=utf-8", body.encode("utf-8"), False)


# 5. Non-allowlisted host is never passed to the fetcher; 200 html -> one doc; non-html skipped


def test_non_allowlisted_host_is_never_fetched():
    fetcher = _FakeFetcher({})
    adapter = SitePagesAdapter(
        urls=["https://evil.test/page", "http://flop.finance/insecure"],
        fetcher=fetcher,
        allowed_hosts=[ALLOWED_HOST],
    )
    docs = list(adapter.iter_documents())
    assert docs == []
    assert fetcher.calls == []  # never called: neither URL is https+allowlisted
    stats = adapter.stats()
    assert stats.read == 2
    assert stats.skipped_filtered == 2


def test_allowlisted_200_html_becomes_one_doc_with_title():
    url = "https://flop.finance/yellowpaper"
    fetcher = _FakeFetcher({url: _html_response(HTML_PAGE)})
    adapter = SitePagesAdapter(urls=[url], fetcher=fetcher, allowed_hosts=[ALLOWED_HOST])
    docs = list(adapter.iter_documents())
    assert len(docs) == 1
    doc = docs[0]
    assert doc.url == url
    assert doc.kind == "site"
    assert doc.content_type == "html"
    assert doc.content == HTML_PAGE  # raw HTML, not pre-extracted
    assert doc.title == "FLOP Yellow Paper"
    prov = doc.provenance_dict()
    assert prov["status"] == "200"
    assert prov["url"] == url
    assert fetcher.calls == [url]


def test_non_html_response_is_skipped():
    url = "https://flop.finance/data.json"
    fetcher = _FakeFetcher(
        {url: FetchResponse(200, "application/json", b'{"a": 1}', False)}
    )
    adapter = SitePagesAdapter(urls=[url], fetcher=fetcher, allowed_hosts=[ALLOWED_HOST])
    docs = list(adapter.iter_documents())
    assert docs == []
    assert adapter.stats().skipped_filtered == 1


def test_custom_kind_is_used():
    url = "https://flop.finance/page"
    fetcher = _FakeFetcher({url: _html_response(HTML_PAGE)})
    adapter = SitePagesAdapter(
        urls=[url], fetcher=fetcher, allowed_hosts=[ALLOWED_HOST], kind="flop_site",
    )
    docs = list(adapter.iter_documents())
    assert docs[0].kind == "flop_site"


def test_non_200_and_truncated_are_skipped_with_the_right_counters():
    ok_url = "https://flop.finance/ok"
    err_url = "https://flop.finance/missing"
    big_url = "https://flop.finance/big"
    fetcher = _FakeFetcher(
        {
            ok_url: _html_response(HTML_PAGE),
            err_url: FetchResponse(404, "text/html", b"nope", False),
            big_url: FetchResponse(200, "text/html", b"x" * 10, True),
        }
    )
    adapter = SitePagesAdapter(
        urls=[ok_url, err_url, big_url], fetcher=fetcher, allowed_hosts=[ALLOWED_HOST],
    )
    docs = list(adapter.iter_documents())
    assert len(docs) == 1
    stats = adapter.stats()
    assert stats.skipped_malformed == 1
    assert stats.skipped_too_large == 1
