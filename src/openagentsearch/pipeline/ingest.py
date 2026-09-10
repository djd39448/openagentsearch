"""Live ingestion: fetch ONE allowlisted URL politely and carry it all the way to searchable rows.

Composition, in order, for LiveIngester.ingest(url):
    scheme + host checks (no network) -> robots.txt for the host (fetched once per host, cached)
    -> PageBudget.allow(host) -> HostRateLimiter.wait(host) -> bounded HTTP GET (no redirects
    are followed, so a hop off the allowlist is impossible) -> RawStore.put (raw bytes +
    provenance line) -> extract() -> DedupingExtractStore.put (extracted JSON, or None when the
    text was seen before) -> index_document() (atomic per document).

Everything before the GET is a refusal that touches no network for the page itself; robots.txt
is the only request made for a host that then turns out to be disallowed. The allowlist is the
outer boundary: a host that is not in it is never contacted, not even for robots.txt.
Unavailable robots.txt (5xx or a transport failure) fails CLOSED for that host.

Provenance vs index: raw bytes and the provenance line are written before indexing and kept
even when indexing fails (they are the evidence of what was fetched). The extracted record is
written before indexing so the dedupe scan sees it; if indexing then raises, that extracted
record is removed again so a retry is not blocked by its own failed attempt. The document hash
is the same everywhere: RawStore hashes the body bytes, index_document() hashes the decoded
text re-encoded as UTF-8, and the decode is strict, so the two hashes are asserted equal.

No production crawl loop lives here: this module ingests URLs it is handed, one at a time,
and reports what happened to each. Nothing here spends money, follows a link, or opens a
socket to a host outside the allowlist.
"""

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from openagentsearch.extract.dedupe import DedupingExtractStore
from openagentsearch.extract.html import extract
from openagentsearch.fetch.allowlist import AllowlistEntry
from openagentsearch.fetch.budget import PageBudget
from openagentsearch.fetch.ratelimit import HostRateLimiter
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.fetch.robots import RobotsPolicy
from openagentsearch.pipeline.index import Embedder, index_document
from openagentsearch.vector.store import VectorStore

USER_AGENT = "OpenAgentSearch-crawler/1.0"

OUTCOMES = (
    "indexed",  # fetched, stored, extracted, and all chunk rows written
    "already_indexed",  # vector rows for this document already existed; extracted record (re)written
    "deduplicated",  # extracted text identical to a stored document; raw kept, nothing indexed
    "refused_scheme",  # not http/https; no network
    "refused_allowlist",  # host not in the allowlist; no network
    "refused_robots",  # robots.txt for the host disallows this path; page not fetched
    "refused_robots_unavailable",  # robots.txt 5xx or unreachable; fail closed; page not fetched
    "refused_budget",  # PageBudget for the host exhausted (STOP-<host> marker written); page not fetched
    "fetch_error",  # transport failure or timeout on the page GET
    "http_error",  # page GET answered with a non-200 status (redirects are not followed)
    "refused_too_large",  # body exceeded max_bytes; nothing stored
    "refused_content_type",  # Content-Type declared and not text/html; nothing stored
    "refused_not_utf8",  # body is not valid UTF-8; nothing stored
    "error",  # ingest_many only: ingest() raised; see detail
)


@dataclass(frozen=True)
class FetchResponse:
    status: int
    content_type: str
    body: bytes
    truncated: bool


Fetcher = Callable[[str, float, int, str], FetchResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def urllib_fetch(url: str, timeout_s: float, max_bytes: int, user_agent: str) -> FetchResponse:
    """Standard-library GET that never follows a redirect and reads at most max_bytes + 1 bytes.

    A 3xx/4xx/5xx answer is returned as a FetchResponse with that status (body included, bounded);
    only transport failures raise (urllib.error.URLError, socket.timeout, OSError).
    """
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "text/html"})
    try:
        response = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as answered:
        response = answered
    with response:
        status = int(response.getcode() or 0)
        content_type = str(response.headers.get("Content-Type", "") or "")
        body = response.read(max_bytes + 1)
    return FetchResponse(status=status, content_type=content_type, body=body[:max_bytes], truncated=len(body) > max_bytes)


@dataclass(frozen=True)
class IngestReport:
    url: str
    host: str
    outcome: str
    status: Optional[int] = None
    doc_sha256: Optional[str] = None
    chunks_indexed: int = 0
    detail: str = ""


class LiveIngester:
    def __init__(
        self,
        *,
        root: Path,
        allowlist: Sequence[AllowlistEntry],
        store: VectorStore,
        embedder: Embedder,
        chunk_size: int,
        overlap: int,
        min_interval_s: float = 1.0,
        timeout_s: float = 10.0,
        max_bytes: int = 2_000_000,
        user_agent: str = USER_AGENT,
        fetcher: Optional[Fetcher] = None,
        clock: Callable[[], float] = time.time,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not allowlist:
            raise ValueError("allowlist must not be empty: an ingester with no hosts has nothing it may fetch")
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and overlap must be in [0, chunk_size)")
        if max_bytes <= 0 or timeout_s <= 0 or min_interval_s < 0:
            raise ValueError("max_bytes and timeout_s must be positive, min_interval_s non-negative")
        self.root = Path(root)
        self.hosts: Dict[str, int] = {}
        for entry in allowlist:
            if not isinstance(entry, AllowlistEntry) or not entry.host or entry.max_pages <= 0:
                raise ValueError(f"invalid allowlist entry: {entry!r}")
            self.hosts[entry.host.lower()] = entry.max_pages
        self.store = store
        self.embedder = embedder
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.fetcher: Fetcher = fetcher or urllib_fetch
        self.clock = clock
        self.raw = RawStore(self.root)
        self.extracted = DedupingExtractStore(self.root)
        self.budget = PageBudget(self.root, dict(self.hosts))
        self.limiter = HostRateLimiter(min_interval_s, sleep=sleep)
        self._robots: Dict[str, Optional[RobotsPolicy]] = {}

    # -- policy -------------------------------------------------------------------------------

    def _robots_for(self, scheme: str, netloc: str, host: str) -> Optional[RobotsPolicy]:
        """robots.txt for a host, fetched once and cached. None means unavailable (fail closed)."""
        if host in self._robots:
            return self._robots[host]
        policy: Optional[RobotsPolicy]
        self.limiter.wait(host)
        try:
            answer = self.fetcher(f"{scheme}://{netloc}/robots.txt", self.timeout_s, self.max_bytes, self.user_agent)
        except Exception:  # transport failure of any kind: fail closed for this host
            policy = None
        else:
            if answer.status == 200 and not answer.truncated:
                policy = RobotsPolicy(answer.body.decode("utf-8", errors="replace"), self.user_agent)
            elif 400 <= answer.status < 500:
                policy = RobotsPolicy("", self.user_agent)  # no robots.txt: everything is allowed
            else:
                policy = None
        self._robots[host] = policy
        return policy

    # -- ingestion ----------------------------------------------------------------------------

    def ingest(self, url: str) -> IngestReport:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https"):
            return IngestReport(url, host, "refused_scheme", detail=f"scheme {parsed.scheme!r}")
        if not host or host not in self.hosts:
            return IngestReport(url, host, "refused_allowlist", detail="host not in allowlist; not contacted")

        policy = self._robots_for(parsed.scheme, parsed.netloc, host)
        if policy is None:
            return IngestReport(url, host, "refused_robots_unavailable", detail="robots.txt unavailable; failing closed")
        if not policy.is_allowed(url):
            return IngestReport(url, host, "refused_robots", detail="disallowed by robots.txt")

        if not self.budget.allow(host):
            return IngestReport(url, host, "refused_budget", detail=f"max_pages {self.hosts[host]} reached for {host}")

        self.limiter.wait(host)
        fetched_at = self.clock()
        try:
            answer = self.fetcher(url, self.timeout_s, self.max_bytes, self.user_agent)
        except Exception as exc:  # transport failure: report, do not raise
            return IngestReport(url, host, "fetch_error", detail=f"{type(exc).__name__}: {exc}")
        if answer.status != 200:
            return IngestReport(url, host, "http_error", status=answer.status, detail="non-200 status; nothing stored")
        if answer.truncated:
            return IngestReport(url, host, "refused_too_large", status=200, detail=f"body exceeds max_bytes {self.max_bytes}")
        media = answer.content_type.split(";", 1)[0].strip().lower()
        if media and media != "text/html":
            return IngestReport(url, host, "refused_content_type", status=200, detail=f"Content-Type {media!r}")
        try:
            html = answer.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            return IngestReport(url, host, "refused_not_utf8", status=200, detail=str(exc))

        doc_sha256 = self.raw.put(url, answer.body, answer.status, True, fetched_at)
        extracted = extract(html)
        extracted_path = self.extracted.put(doc_sha256, url, extracted, self.clock())
        if extracted_path is None:
            return IngestReport(url, host, "deduplicated", status=200, doc_sha256=doc_sha256,
                                detail="extracted text already stored under another document")
        try:
            report = index_document(
                html, url, store=self.store, embedder=self.embedder, chunk_size=self.chunk_size, overlap=self.overlap
            )
        except ValueError as exc:
            if "already indexed" in str(exc):
                return IngestReport(url, host, "already_indexed", status=200, doc_sha256=doc_sha256, detail=str(exc))
            extracted_path.unlink(missing_ok=True)
            raise
        except Exception:
            extracted_path.unlink(missing_ok=True)
            raise
        if report.doc_sha256 != doc_sha256:
            extracted_path.unlink(missing_ok=True)
            raise RuntimeError(f"document hash disagreement: raw {doc_sha256} vs index {report.doc_sha256}")
        return IngestReport(url, host, "indexed", status=200, doc_sha256=doc_sha256, chunks_indexed=report.chunks_indexed)

    def ingest_many(self, urls: Iterable[str]) -> List[IngestReport]:
        """Ingest URLs in order. A raise inside ingest() (embedder/store failure) becomes an
        'error' report and the batch continues; refusals and fetch failures are ordinary reports."""
        reports: List[IngestReport] = []
        for url in urls:
            try:
                reports.append(self.ingest(url))
            except Exception as exc:
                host = (urlparse(url).hostname or "").lower() if isinstance(url, str) else ""
                reports.append(IngestReport(str(url), host, "error", detail=f"{type(exc).__name__}: {exc}"))
        return reports
