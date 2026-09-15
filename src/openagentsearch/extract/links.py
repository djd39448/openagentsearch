"""Pure link extraction and host/path allow-checking for the bounded crawl loop.

`extract_links` finds `<a href>` targets in an HTML string and resolves them against a base URL;
`url_allowed` checks a URL against a mapping of `openagentsearch.pipeline.crawlconfig.HostRule`.
Both are pure -- no I/O, no network, and no dependency on anything already fetched.

NOT guaranteed: `extract_links` is not a browser. It parses only `<a href="...">` -- it does not
run JavaScript, does not follow `<link>` or `<iframe>` elements, and does not itself look for a
page's own `<base href>` (a link is resolved against whatever `base_url` the caller passes, via
`urllib.parse.urljoin`; if a caller wants `<base href>` honoured, it must find that value itself
and pass it as `base_url`). It has no notion of `rel="nofollow"` or robots meta tags -- policy like
that is enforced elsewhere (robots.txt is `LiveIngester`'s job, not this module's).
"""

import html.parser
from collections.abc import Mapping
from urllib.parse import urljoin, urlparse

from openagentsearch.pipeline.crawlconfig import HostRule

_MAX_LINKS_FLOOR = 1


class _LinkParser(html.parser.HTMLParser):
    """Collects every `href` attribute value off `<a>` start tags, in document order, exactly as
    written (no resolution, no filtering -- that happens in `extract_links`)."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)
                return


def extract_links(html_text: str, base_url: str, *, max_links: int = 200) -> list[str]:
    """Every distinct, resolvable `http`/`https` link out of `<a href>` tags in `html_text`,
    resolved against `base_url`, in first-seen order, at most `max_links` entries.

    Each href is resolved with `urllib.parse.urljoin(base_url, href)`, then normalized: scheme and
    host are lowercased, the fragment is dropped, and the query string (if any) is kept as-is.
    `mailto:` and `javascript:` hrefs (case-insensitively) are skipped before resolution, as is any
    href that resolves to a scheme other than `http`/`https` or to no host at all. Deduplication is
    by the normalized URL, keeping the first occurrence; `max_links` bounds the length of the
    returned list, not the number of `<a>` tags scanned.

    NOT guaranteed: see the module docstring for what this does NOT parse or honour (no JS, no
    `<link>`/`<iframe>`, no automatic `<base href>` handling). An href that is empty, whitespace
    only, or an `<a>` tag with no `href` attribute at all is skipped, not counted as a link.
    """
    if not isinstance(html_text, str):
        raise ValueError("html_text must be a string")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("base_url must be a non-empty string")
    bad_max_links = (
        isinstance(max_links, bool)
        or not isinstance(max_links, int)
        or max_links < _MAX_LINKS_FLOOR
    )
    if bad_max_links:
        raise ValueError(f"max_links must be an int >= {_MAX_LINKS_FLOOR}, got {max_links!r}")

    parser = _LinkParser()
    parser.feed(html_text)

    seen: set[str] = set()
    result: list[str] = []
    for href in parser.hrefs:
        stripped = href.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("mailto:") or lowered.startswith("javascript:"):
            continue
        resolved = urljoin(base_url, stripped)
        parsed = urlparse(resolved)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if not host:
            continue
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        normalized = f"{scheme}://{netloc}{parsed.path}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= max_links:
            break
    return result


def url_allowed(url: str, rules: Mapping[str, HostRule]) -> bool:
    """`True` when `url` is `http`/`https`, its host is a key in `rules`, and -- when that host's
    `HostRule.path_prefixes` is non-empty -- its path starts with one of those prefixes. A host
    rule with no prefixes at all allows every path on that host.

    Pure: does not consult any budget, robots policy, or visited/frontier state -- those are the
    crawl loop's concerns, not this function's. A URL that fails to parse a host at all (or has
    none) is never allowed.
    """
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    rule = rules.get(host)
    if rule is None:
        return False
    if not rule.path_prefixes:
        return True
    return any(parsed.path.startswith(prefix) for prefix in rule.path_prefixes)
