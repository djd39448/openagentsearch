"""Package A4: `openagentsearch.extract.links` -- pure link extraction and host/path allow-check."""

import pytest

from openagentsearch.extract.links import extract_links, url_allowed
from openagentsearch.pipeline.crawlconfig import HostRule


def test_relative_and_absolute_resolution():
    html = (
        '<a href="/a.html">a</a>'
        '<a href="b.html">b</a>'
        '<a href="http://other.test/c.html">c</a>'
    )
    links = extract_links(html, "http://example.test/dir/page.html")
    assert links == [
        "http://example.test/a.html",
        "http://example.test/dir/b.html",
        "http://other.test/c.html",
    ]


def test_fragment_is_dropped_but_query_is_kept():
    html = '<a href="/x.html?y=1#section-2">x</a>'
    links = extract_links(html, "http://example.test/")
    assert links == ["http://example.test/x.html?y=1"]


def test_mailto_and_javascript_are_skipped_case_insensitively():
    html = (
        '<a href="mailto:someone@example.test">mail</a>'
        '<a href="MAILTO:someone@example.test">mail2</a>'
        '<a href="javascript:doThing()">js</a>'
        '<a href="JavaScript:doThing()">js2</a>'
        '<a href="/real.html">real</a>'
    )
    assert extract_links(html, "http://example.test/") == ["http://example.test/real.html"]


def test_scheme_and_host_are_lowercased_and_dedupe_keeps_first_seen_order():
    html = (
        '<a href="HTTP://EXAMPLE.test/A.html">first</a>'
        '<a href="http://example.TEST/A.html">dup of first (same normalized url)</a>'
        '<a href="/B.html">second</a>'
        '<a href="/A.html">not a dup: different base-relative path</a>'
    )
    links = extract_links(html, "http://example.test/")
    assert links == [
        "http://example.test/A.html",
        "http://example.test/B.html",
    ]
    assert len(links) == len(set(links))


def test_non_http_schemes_and_hrefless_anchors_are_skipped():
    html = (
        '<a href="ftp://example.test/f">ftp</a>'
        '<a href="ws://example.test/w">ws</a>'
        '<a href="data:text/plain;base64,aGk=">data</a>'
        '<a href="tel:+15551234567">tel</a>'
        '<a href="">empty</a>'
        '<a href="   ">whitespace-only</a>'
        '<a>no href at all</a>'
        '<a href="/ok.html">ok</a>'
    )
    assert extract_links(html, "http://example.test/") == ["http://example.test/ok.html"]


def test_a_url_that_resolves_to_no_host_is_skipped():
    # An absolute-scheme href with no netloc of its own does NOT inherit the base's host when its
    # scheme differs from the base's (https:// here vs. http:// for the base) -- urljoin leaves it
    # with an empty netloc, which this function must treat as "no host" rather than crash on.
    html = '<a href="https://">x</a><a href="/keep.html">keep</a>'
    assert extract_links(html, "http://example.test/") == ["http://example.test/keep.html"]


def test_max_links_caps_the_returned_list_not_the_scan():
    html = "".join(f'<a href="/p{i}.html">p{i}</a>' for i in range(10))
    links = extract_links(html, "http://example.test/", max_links=3)
    assert links == [
        "http://example.test/p0.html",
        "http://example.test/p1.html",
        "http://example.test/p2.html",
    ]


def test_only_a_tags_and_only_href_attribute_are_considered():
    html = (
        '<link rel="stylesheet" href="/style.css">'
        '<iframe src="/frame.html"></iframe>'
        '<a data-href="/decoy.html">decoy</a>'
        '<a href="/real.html">real</a>'
    )
    assert extract_links(html, "http://example.test/") == ["http://example.test/real.html"]


def test_extract_links_input_validation():
    with pytest.raises(ValueError):
        extract_links(123, "http://example.test/")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        extract_links("<a href='/x'>x</a>", "")
    with pytest.raises(ValueError):
        extract_links("<a href='/x'>x</a>", "http://example.test/", max_links=0)


# -------------------------------------------------------------------------------------- url_allowed


def test_url_allowed_without_prefixes_allows_every_path_on_the_host():
    rules = {"example.test": HostRule("example.test", 10, ())}
    assert url_allowed("https://example.test/anything/at/all", rules)
    assert url_allowed("http://example.test/", rules)
    assert not url_allowed("https://other.test/anything", rules)


def test_url_allowed_with_prefixes_requires_a_path_match():
    rules = {"example.test": HostRule("example.test", 10, ("/docs", "/r/builders"))}
    assert url_allowed("https://example.test/docs/page.html", rules)
    assert url_allowed("https://example.test/docs", rules)  # exact prefix match
    assert url_allowed("https://example.test/r/builders/thread-1", rules)
    assert not url_allowed("https://example.test/other/page.html", rules)
    assert not url_allowed("https://example.test/", rules)


def test_url_allowed_rejects_non_http_schemes_and_unknown_hosts():
    rules = {"example.test": HostRule("example.test", 10, ())}
    assert not url_allowed("ftp://example.test/x", rules)
    assert not url_allowed("https://unknown.test/x", rules)
    assert not url_allowed("not-a-url-at-all", rules)
    assert not url_allowed("", rules)


def test_url_allowed_host_lookup_is_case_insensitive():
    rules = {"example.test": HostRule("example.test", 10, ())}
    assert url_allowed("https://EXAMPLE.test/page", rules)
