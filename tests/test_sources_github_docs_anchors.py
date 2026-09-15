"""Repeated headings inside one markdown file must yield distinct section URLs.

The index manifest keys documents by `source_url`; two sections that share a URL make the later
one supersede the earlier one. The first live run marked 78 of 523 GitHub-doc sections superseded
for exactly this reason (every CHANGELOG entry has an "Added" section). GitHub disambiguates
repeated anchors as `slug`, `slug-1`, `slug-2`; the adapter now does the same.
"""

from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.sources.github_docs import GitHubRepoDocsAdapter

_BODY = (
    "# Changelog\n\n"
    "## 0.2.0\n\n### Added\n\n" + ("alpha " * 60) + "\n\n"
    "## 0.1.0\n\n### Added\n\n" + ("beta " * 60) + "\n\n"
    "## 0.0.1\n\n### Added\n\n" + ("gamma " * 60) + "\n"
)


def _fetcher(url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
    return FetchResponse(200, "text/plain; charset=utf-8", _BODY.encode("utf-8"), False)


def test_repeated_headings_get_unique_github_style_anchors() -> None:
    adapter = GitHubRepoDocsAdapter(
        owner="o", repo="r", commit="a" * 40, paths=["CHANGELOG.md"], fetcher=_fetcher
    )
    docs = list(adapter.iter_documents())
    urls = [doc.url for doc in docs]
    assert len(urls) == len(set(urls)), urls
    added = [u for u in urls if "#added" in u]
    assert len(added) == 3, urls
    assert added[0].endswith("#added")
    assert added[1].endswith("#added-1")
    assert added[2].endswith("#added-2")
    # the section text still belongs to the right anchor
    by_url = {doc.url: doc.content for doc in docs}
    assert "alpha" in by_url[added[0]] and "gamma" in by_url[added[2]]


def test_distinct_headings_keep_plain_anchors() -> None:
    body = "# T\n\n## Alpha\n\n" + ("a " * 120) + "\n\n## Beta\n\n" + ("b " * 120) + "\n"

    def fetcher(url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
        return FetchResponse(200, "text/plain; charset=utf-8", body.encode("utf-8"), False)

    adapter = GitHubRepoDocsAdapter(
        owner="o", repo="r", commit="b" * 40, paths=["doc.md"], fetcher=fetcher
    )
    urls = [doc.url for doc in adapter.iter_documents()]
    assert any(u.endswith("#alpha") for u in urls) and any(u.endswith("#beta") for u in urls)
    assert not any(u.endswith("-1") for u in urls)
