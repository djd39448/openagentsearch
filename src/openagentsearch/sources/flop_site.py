"""Adapter for an explicit, operator-supplied list of URLs -- FLOP's own site pages, or anything
else a human decides belongs in the index by hand -- fetched through an injected fetcher with a
strict host allowlist and no redirect-following.

No robots.txt handling lives here: this adapter is for a short, explicit, operator-curated URL
list (the kind of thing a human pastes in once), not a crawl. The A4 crawl loop (not part of this
package) is where robots.txt, page budgets and rate limiting belong for anything that discovers
URLs on its own; using this adapter to fetch a large or machine-discovered URL list would bypass
all of that.

NOT guaranteed: a URL whose host is allowlisted but that 404s, redirects, or returns non-HTML is
simply skipped with the matching counter -- this adapter does not retry, follow the redirect
itself, or distinguish "host not allowed" from "scheme not https" in `AdapterStats` (both are
`skipped_filtered`, since in both cases the fetcher is never called).
"""

import hashlib
import time
from collections.abc import Callable, Sequence
from typing import Iterator
from urllib.parse import urlparse

from openagentsearch.extract.html import extract
from openagentsearch.pipeline.ingest import USER_AGENT, Fetcher
from openagentsearch.sources.base import AdapterStats, SourceDoc


class SitePagesAdapter:
    """`SourceAdapter` over an explicit list of URLs. `kind` defaults to `"site"` but is
    caller-settable so a deployment can distinguish, e.g., `"flop_site"` from other explicit
    lists."""

    name = "flop_site_pages"

    def __init__(
        self,
        *,
        urls: Sequence[str],
        fetcher: Fetcher,
        allowed_hosts: Sequence[str],
        kind: str = "site",
        timeout_s: float = 10.0,
        max_bytes: int = 2_000_000,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.urls = list(urls)
        self.fetcher = fetcher
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.kind = kind
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.clock = clock
        self._read = 0
        self._yielded = 0
        self._skipped_malformed = 0
        self._skipped_filtered = 0
        self._skipped_too_large = 0

    def stats(self) -> AdapterStats:
        return AdapterStats(
            read=self._read,
            yielded=self._yielded,
            skipped_malformed=self._skipped_malformed,
            skipped_filtered=self._skipped_filtered,
            skipped_too_large=self._skipped_too_large,
        )

    def iter_documents(self) -> Iterator[SourceDoc]:
        for url in self.urls:
            self._read += 1
            try:
                parsed = urlparse(url)
            except ValueError:
                # a URL that cannot even be parsed enough to check scheme/host is treated the
                # same as one that fails the allowlist check: never fetched.
                self._skipped_filtered += 1
                continue
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or host not in self.allowed_hosts:
                self._skipped_filtered += 1
                continue  # never fetched: host allowlist is the outer boundary

            fetched_at = self.clock()
            try:
                answer = self.fetcher(url, self.timeout_s, self.max_bytes, self.user_agent)
            except Exception:
                self._skipped_malformed += 1
                continue
            if answer.status != 200:
                self._skipped_malformed += 1
                continue
            if answer.truncated:
                self._skipped_too_large += 1
                continue
            media = answer.content_type.split(";", 1)[0].strip().lower()
            if media != "text/html":
                self._skipped_filtered += 1
                continue
            try:
                html = answer.body.decode("utf-8")
            except UnicodeDecodeError:
                self._skipped_malformed += 1
                continue
            if not html:
                self._skipped_malformed += 1
                continue

            extracted = extract(html)
            sha256 = hashlib.sha256(answer.body).hexdigest()
            provenance = tuple(
                sorted(
                    {
                        "url": url,
                        "status": str(answer.status),
                        "fetched_at": str(fetched_at),
                        "sha256": sha256,
                        "content_type": answer.content_type,
                    }.items()
                )
            )
            self._yielded += 1
            yield SourceDoc(
                url=url,
                kind=self.kind,
                content=html,
                content_type="html",
                fetched_at=fetched_at,
                provenance=provenance,
                title=extracted["title"] or None,
            )
