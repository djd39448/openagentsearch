"""Pure loader for the bounded crawl loop's config file (`config/flop.yaml`).

`load_crawl_config(path)` reads one YAML file into a validated, immutable `CrawlConfig`: host
rules (allowlisted host, page budget, optional path prefixes), seed URLs, and the FLOP source
descriptors (room directory path, pinned-commit GitHub docs, GitHub issue repos, explicit site
pages) consumed by `openagentsearch.pipeline.crawl`. Every dataclass validates itself in
`__post_init__`, the same convention `openagentsearch.sources.base.SourceDoc` and
`openagentsearch.index.manifest.ManifestEntry` use elsewhere in this repository: an instance that
exists is guaranteed well-formed, and every rejection is a `ValueError` naming the offending field.

NOT guaranteed: this module never fetches anything and never checks that a seed URL, a GitHub
repository, or a `rooms_jsonl` path is actually reachable -- it only validates shape. A host that
looks like an IP literal is accepted ONLY as the exact string `"127.0.0.1"` (the crawl loop's own
tests run a real server there); every other IP literal is rejected, and reaching a private address
through a *hostname* that merely resolves to one is a DNS-time concern this module cannot see and
does not attempt to guard against.
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from openagentsearch.fetch.allowlist import AllowlistEntry

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_LOOPBACK_LITERAL = "127.0.0.1"


def _validate_host(host: object) -> str:
    if not isinstance(host, str) or not host:
        raise ValueError(f"hosts: each host key must be a non-empty string, got {host!r}")
    if host != host.lower():
        raise ValueError(f"hosts: host {host!r} must be lowercase")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True
    if is_ip_literal:
        if host != _LOOPBACK_LITERAL:
            raise ValueError(
                f"hosts: {host!r} is an IP literal; only {_LOOPBACK_LITERAL!r} is accepted "
                "(reserved for the test loopback server)"
            )
        return host
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"hosts: {host!r} is not a valid lowercase hostname")
    return host


@dataclass(frozen=True)
class HostRule:
    """One allowlisted host: its per-run page budget and, optionally, the URL path prefixes the
    crawl loop is confined to on that host (an empty tuple means every path is in scope)."""

    host: str
    max_pages: int
    path_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_host(self.host)
        if isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int):
            raise ValueError(f"hosts.{self.host}.max_pages must be an int, got {self.max_pages!r}")
        if self.max_pages <= 0:
            raise ValueError(f"hosts.{self.host}.max_pages must be > 0, got {self.max_pages}")
        if not isinstance(self.path_prefixes, tuple):
            raise ValueError(f"hosts.{self.host}.path_prefixes must be a tuple of strings")
        for prefix in self.path_prefixes:
            if not isinstance(prefix, str) or not prefix.startswith("/"):
                raise ValueError(
                    f"hosts.{self.host}.path_prefixes entry must start with '/', got {prefix!r}"
                )


@dataclass(frozen=True)
class GitHubDocsSource:
    """One pinned-commit repository to read markdown/text files from (package A3's
    `GitHubRepoDocsAdapter`, fed by the crawl loop's own tree-listing fetch)."""

    owner: str
    repo: str
    commit: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not _NAME_RE.match(self.owner):
            raise ValueError(
                f"sources.github_docs.owner must match {_NAME_RE.pattern!r}, got {self.owner!r}"
            )
        if not isinstance(self.repo, str) or not _NAME_RE.match(self.repo):
            raise ValueError(
                f"sources.github_docs.repo must match {_NAME_RE.pattern!r}, got {self.repo!r}"
            )
        if not isinstance(self.commit, str) or not _COMMIT_RE.match(self.commit):
            raise ValueError(
                "sources.github_docs.commit must be 40 lowercase hex characters, "
                f"got {self.commit!r}"
            )


@dataclass(frozen=True)
class GitHubIssuesSource:
    """One repository to read issues + comments from (package A3's `GitHubIssuesAdapter`, fed by
    `load_issues_with_gh`)."""

    owner: str
    repo: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not _NAME_RE.match(self.owner):
            raise ValueError(
                f"sources.github_issues.owner must match {_NAME_RE.pattern!r}, got {self.owner!r}"
            )
        if not isinstance(self.repo, str) or not _NAME_RE.match(self.repo):
            raise ValueError(
                f"sources.github_issues.repo must match {_NAME_RE.pattern!r}, got {self.repo!r}"
            )


@dataclass(frozen=True)
class CrawlConfig:
    """The whole validated config file: host rules, crawl seeds, and FLOP source descriptors.

    NOT guaranteed: seed validation only checks that each seed is `https://` and that its host
    appears in `hosts` -- it does NOT check the seed's path against that host's `path_prefixes`
    (a seed outside its host's prefixes is simply never fetched, refused the same way any other
    out-of-prefix URL is by `openagentsearch.extract.links.url_allowed`).
    """

    hosts: tuple[HostRule, ...]
    seeds: tuple[str, ...]
    rooms_jsonl: str | None
    github_docs: tuple[GitHubDocsSource, ...]
    github_issues: tuple[GitHubIssuesSource, ...]
    site_pages: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.hosts, tuple) or not self.hosts:
            raise ValueError("hosts must be a non-empty tuple of HostRule")
        if not all(isinstance(h, HostRule) for h in self.hosts):
            raise ValueError("hosts must contain only HostRule instances")
        seen_hosts: set[str] = set()
        for rule in self.hosts:
            if rule.host in seen_hosts:
                raise ValueError(f"hosts: duplicate host {rule.host!r}")
            seen_hosts.add(rule.host)
        if not isinstance(self.seeds, tuple) or not all(isinstance(s, str) for s in self.seeds):
            raise ValueError("seeds must be a tuple of strings")
        for i, seed in enumerate(self.seeds):
            parsed = urlparse(seed)
            host = (parsed.hostname or "").lower()
            # https is required for every real seed; the one narrow exception is the test
            # loopback literal (127.0.0.1), which this module's own HostRule validation already
            # restricts to test use -- a real deployment's config file can never declare it.
            scheme_ok = parsed.scheme == "https" or host == _LOOPBACK_LITERAL
            if not scheme_ok or host not in seen_hosts:
                raise ValueError(
                    f"seeds[{i}] {seed!r} must be https (or on the {_LOOPBACK_LITERAL!r} test "
                    f"host) and on an allowlisted host (seed host: {host!r})"
                )
        if self.rooms_jsonl is not None and not isinstance(self.rooms_jsonl, str):
            raise ValueError("sources.rooms_jsonl must be a string or null")
        if not isinstance(self.github_docs, tuple) or not all(
            isinstance(g, GitHubDocsSource) for g in self.github_docs
        ):
            raise ValueError("sources.github_docs must be a tuple of GitHubDocsSource")
        if not isinstance(self.github_issues, tuple) or not all(
            isinstance(g, GitHubIssuesSource) for g in self.github_issues
        ):
            raise ValueError("sources.github_issues must be a tuple of GitHubIssuesSource")
        if not isinstance(self.site_pages, tuple) or not all(
            isinstance(s, str) and s for s in self.site_pages
        ):
            raise ValueError("sources.site_pages must be a tuple of non-empty strings")


def _require_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def load_crawl_config(path: str | Path) -> CrawlConfig:
    """Load and validate one crawl config YAML file. Raises `ValueError` (with the offending
    field named) for anything malformed; never partially returns a config."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError("crawl config YAML must be a mapping at the top level")

    hosts_raw = _require_mapping(raw.get("hosts"), "hosts")
    if not hosts_raw:
        raise ValueError("hosts must be a non-empty mapping")
    hosts: list[HostRule] = []
    for host, cfg in hosts_raw.items():
        cfg = _require_mapping(cfg, f"hosts.{host}")
        prefixes_raw = cfg.get("path_prefixes", [])
        if not isinstance(prefixes_raw, list) or not all(isinstance(p, str) for p in prefixes_raw):
            raise ValueError(f"hosts.{host}.path_prefixes must be a list of strings")
        hosts.append(
            HostRule(
                host=host if isinstance(host, str) else str(host),
                max_pages=cfg.get("max_pages"),  # type: ignore[arg-type]
                path_prefixes=tuple(prefixes_raw),
            )
        )

    seeds_raw = raw.get("seeds", [])
    if not isinstance(seeds_raw, list) or not all(isinstance(s, str) for s in seeds_raw):
        raise ValueError("seeds must be a list of strings")

    sources_value = raw.get("sources")
    sources_raw = _require_mapping({} if sources_value is None else sources_value, "sources")

    rooms_jsonl_raw = sources_raw.get("rooms_jsonl")
    if rooms_jsonl_raw is not None and not isinstance(rooms_jsonl_raw, str):
        raise ValueError("sources.rooms_jsonl must be a string or null")

    github_docs_raw = sources_raw.get("github_docs", [])
    if not isinstance(github_docs_raw, list):
        raise ValueError("sources.github_docs must be a list")
    github_docs: list[GitHubDocsSource] = []
    for i, entry in enumerate(github_docs_raw):
        entry = _require_mapping(entry, f"sources.github_docs[{i}]")
        github_docs.append(
            GitHubDocsSource(
                owner=entry.get("owner"),  # type: ignore[arg-type]
                repo=entry.get("repo"),  # type: ignore[arg-type]
                commit=entry.get("commit"),  # type: ignore[arg-type]
            )
        )

    github_issues_raw = sources_raw.get("github_issues", [])
    if not isinstance(github_issues_raw, list):
        raise ValueError("sources.github_issues must be a list")
    github_issues: list[GitHubIssuesSource] = []
    for i, entry in enumerate(github_issues_raw):
        entry = _require_mapping(entry, f"sources.github_issues[{i}]")
        github_issues.append(
            GitHubIssuesSource(
                owner=entry.get("owner"),  # type: ignore[arg-type]
                repo=entry.get("repo"),  # type: ignore[arg-type]
            )
        )

    site_pages_raw = sources_raw.get("site_pages", [])
    if not isinstance(site_pages_raw, list) or not all(isinstance(s, str) for s in site_pages_raw):
        raise ValueError("sources.site_pages must be a list of strings")

    return CrawlConfig(
        hosts=tuple(hosts),
        seeds=tuple(seeds_raw),
        rooms_jsonl=rooms_jsonl_raw,
        github_docs=tuple(github_docs),
        github_issues=tuple(github_issues),
        site_pages=tuple(site_pages_raw),
    )


def allowlist_entries(config: CrawlConfig) -> list[AllowlistEntry]:
    """`config.hosts` reshaped into `openagentsearch.fetch.allowlist.AllowlistEntry` rows, for
    `LiveIngester`."""
    return [AllowlistEntry(host=rule.host, max_pages=rule.max_pages) for rule in config.hosts]


def config_sha256(config: CrawlConfig) -> str:
    """Deterministic content hash of `config` (hosts, seeds and sources only -- never CLI-only
    overrides like `--max-pages-per-host`), used by the crawl loop to refuse `--resume` against a
    saved state from a materially different config."""
    payload = {
        "hosts": [
            {"host": h.host, "max_pages": h.max_pages, "path_prefixes": list(h.path_prefixes)}
            for h in config.hosts
        ],
        "seeds": list(config.seeds),
        "rooms_jsonl": config.rooms_jsonl,
        "github_docs": [
            {"owner": g.owner, "repo": g.repo, "commit": g.commit} for g in config.github_docs
        ],
        "github_issues": [{"owner": g.owner, "repo": g.repo} for g in config.github_issues],
        "site_pages": list(config.site_pages),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
