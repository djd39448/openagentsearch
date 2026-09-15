"""Bounded, config-driven, resumable crawl loop (`python -m openagentsearch.pipeline.crawl`).

Two stages, run in order by `run_crawl()`:

1. **Sources stage** (skipped by `skip_sources=True`): each FLOP source adapter from package A3
   (room directory, pinned-commit GitHub docs, GitHub issues, explicit site pages) is built from
   `CrawlConfig` and drained through `index_source_documents()`. A source failing to build or
   fetch is recorded and never fatal to the crawl -- this stage always re-runs in full on every
   invocation (including a `--resume` one); it has nothing to do with `crawl-state.json`, and
   `index_source_document()`'s own "already indexed" refusal makes re-running it idempotent.
2. **Crawl stage**: breadth-first over `LiveIngester.ingest()`, starting from `CrawlConfig.seeds`
   (plus any `--seed` given), following only links `openagentsearch.extract.links.url_allowed`
   accepts (allowlisted host, in-prefix path). Politeness (robots.txt, per-host rate limiting,
   bounded GET, no redirects) lives entirely inside `LiveIngester`; this loop only decides what to
   fetch next and when to stop.

`CrawlState` is the whole crawl-stage frontier/visited/budget-progress, atomically checkpointed to
`root/crawl-state.json` (write-to-temp + `os.replace`) every `--checkpoint-every` attempted pages
and once more on exit, success or exception. `--resume` requires that file, refuses one saved under
a different config (`config_sha256` mismatch -- computed from `hosts`/`seeds`/`sources` only, never
from `--max-pages-per-host`, so raising the per-host cap on a resumed run is exactly what that flag
is for), and reconstructs each host's remaining budget as `max_pages - fetched_by_host[host]`. A
`--resume` invocation's frontier always comes from that persisted state, never from `config.seeds`,
so `main()` keeps CLI `--seed` values out of the config used for a `--resume` run entirely -- they
would otherwise have no effect on what gets crawled but would still spuriously change
`config_sha256` and refuse the resume.

Nothing here fetches a host outside `CrawlConfig.hosts`, follows a redirect, or spends anything --
those guarantees live in `LiveIngester` and `openagentsearch.extract.links.url_allowed`, which this
loop always calls before adding a discovered link to the frontier.
"""

import argparse
import dataclasses
import json
import os
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from openagentsearch.embed.keyword import KeywordEmbedder
from openagentsearch.embed.ollama import OllamaEmbedClient
from openagentsearch.extract.links import extract_links, url_allowed
from openagentsearch.fetch.allowlist import AllowlistEntry
from openagentsearch.pipeline.crawlconfig import (
    CrawlConfig,
    HostRule,
    config_sha256,
    load_crawl_config,
)
from openagentsearch.pipeline.index import Embedder, SourceIndexReport, index_source_documents
from openagentsearch.pipeline.ingest import (
    USER_AGENT,
    Fetcher,
    FetchResponse,
    LiveIngester,
    urllib_fetch,
)
from openagentsearch.sources.base import AdapterStats, SourceDoc
from openagentsearch.sources.flop_site import SitePagesAdapter
from openagentsearch.sources.github_docs import GitHubRepoDocsAdapter, markdown_paths_from_tree
from openagentsearch.sources.github_issues import GitHubIssuesAdapter, load_issues_with_gh
from openagentsearch.sources.technocore_rooms import RoomDirectoryAdapter
from openagentsearch.vector.store import VectorStore

GhRunner = Callable[[Sequence[str]], bytes]

_DEFAULT_DIMENSION = {"keyword": 256, "ollama": 768}
_DEFAULT_CHUNK_SIZE = 1000
_DEFAULT_OVERLAP = 100
_TREE_MAX_BYTES = 5_000_000
_TREE_TIMEOUT_S = 10.0
_GITHUB_JSON_ACCEPT = "application/vnd.github+json"

# Outcomes that do NOT represent a consumed PageBudget slot: the four pre-budget refusals never
# reach PageBudget.allow(host) at all, and "refused_budget" IS that call -- the one where allow()
# returns False and writes the STOP marker, consuming nothing. See fetched_by_host's docstring on
# CrawlState below for why this distinction is exactly what resume's remaining-budget math needs.
_BUDGET_NOT_CONSUMED_OUTCOMES = frozenset(
    {
        "refused_scheme", "refused_allowlist", "refused_robots", "refused_robots_unavailable",
        "refused_budget",
    }
)


class CrawlResumeError(ValueError):
    """`--resume` was requested but there is no usable prior state: the state file is missing or
    unreadable, or its `config_sha256` does not match the current config. Reported by `main()` as
    exit 2 (a precondition/usage error), distinct from exit 1 for a failure during the crawl
    itself."""


class _GhRunnerDisabled:
    """Sentinel type for `gh_runner`: pass `GH_RUNNER_DISABLED` (the one instance of this class)
    to skip the GitHub issues source entirely, rather than calling `load_issues_with_gh` at all."""


GH_RUNNER_DISABLED: Final = _GhRunnerDisabled()


# -------------------------------------------------------------------------------------- CrawlState


def _typed_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"crawl state field {field!r} must be a list")
    return value


def _str_list(value: object, field: str) -> list[str]:
    items = _typed_list(value, field)
    strings: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"crawl state field {field!r} must be a list of strings")
        strings.append(item)
    return strings


def _str_int_pairs(value: object, field: str) -> list[tuple[str, int]]:
    items = _typed_list(value, field)
    pairs: list[tuple[str, int]] = []
    for item in items:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
        ):
            raise ValueError(f"crawl state field {field!r} must be a list of [str, int] pairs")
        pairs.append((item[0], item[1]))
    return pairs


@dataclass(frozen=True)
class CrawlState:
    """The whole crawl-stage progress needed to resume: what is still queued, what has already
    been handled, per-host budget progress so far, and a fingerprint of the config that produced
    it. Pure (`to_json`/`from_json` only convert shape; no I/O).

    `fetched_by_host[host]` is NOT "pages successfully indexed for host" -- it is how many
    `PageBudget.allow(host)` calls actually returned `True` for that host this run (every outcome
    except the ones in `_BUDGET_NOT_CONSUMED_OUTCOMES`: the four pre-budget refusals, which never
    reach `allow()` at all, and `refused_budget` itself, which IS the one `allow()` call that
    returns `False` and consumes nothing). That is deliberate: a fresh `PageBudget` built on resume
    with `max_pages = original_max_pages - fetched_by_host[host]` then accepts exactly as many more
    `True` calls as the original, single long-lived `PageBudget` would have from this point,
    regardless of how many processes it took to get here.
    """

    frontier: tuple[str, ...]
    visited: tuple[str, ...]
    fetched_by_host: tuple[tuple[str, int], ...]
    config_sha256: str
    pages_attempted: int
    outcomes: tuple[tuple[str, int], ...]

    def to_json(self) -> dict[str, object]:
        return {
            "frontier": list(self.frontier),
            "visited": list(self.visited),
            "fetched_by_host": [[host, count] for host, count in self.fetched_by_host],
            "config_sha256": self.config_sha256,
            "pages_attempted": self.pages_attempted,
            "outcomes": [[name, count] for name, count in self.outcomes],
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> "CrawlState":
        if not isinstance(data, Mapping):
            raise ValueError("crawl state must be a JSON object")
        required = {
            "frontier", "visited", "fetched_by_host", "config_sha256", "pages_attempted",
            "outcomes",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"crawl state missing fields: {sorted(missing)}")
        config_sha256_value = data["config_sha256"]
        if not isinstance(config_sha256_value, str) or not config_sha256_value:
            raise ValueError("crawl state field 'config_sha256' must be a non-empty string")
        pages_attempted = data["pages_attempted"]
        if (
            isinstance(pages_attempted, bool)
            or not isinstance(pages_attempted, int)
            or pages_attempted < 0
        ):
            raise ValueError("crawl state field 'pages_attempted' must be a non-negative int")
        return CrawlState(
            frontier=tuple(_str_list(data["frontier"], "frontier")),
            visited=tuple(_str_list(data["visited"], "visited")),
            fetched_by_host=tuple(_str_int_pairs(data["fetched_by_host"], "fetched_by_host")),
            config_sha256=config_sha256_value,
            pages_attempted=pages_attempted,
            outcomes=tuple(_str_int_pairs(data["outcomes"], "outcomes")),
        )


def _state_path(root: Path) -> Path:
    return root / "crawl-state.json"


def _write_state_atomic(root: Path, state: CrawlState) -> None:
    path = _state_path(root)
    tmp = root / "crawl-state.json.tmp"
    tmp.write_text(json.dumps(state.to_json(), separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------- CrawlReport


@dataclass(frozen=True)
class CrawlReport:
    """What the crawl stage has done as of the end of this `run_crawl()` invocation.
    `pages_attempted`, `outcomes`, `indexed` and `hosts_stopped` mirror `CrawlState` exactly: they
    are CUMULATIVE across every resumed run that produced the current `crawl-state.json`, not just
    this process's own attempts -- the same convention the persisted state itself uses, so a
    `--resume` report reads as "the crawl's progress so far", not "what just happened in this
    process". A fresh
    (non-`--resume`) run's numbers are, naturally, cumulative-so-far == this-run-only, since there
    is no prior state to carry forward. `sources`/`source_index` describe only THIS invocation's
    sources stage (it always re-runs in full, `--resume` or not) and are `None` exactly when
    `skip_sources=True`; otherwise `sources` is present (possibly empty) even when no source was
    configured at all. `source_errors` lists `(label, "ExceptionType: message")` for every source
    whose adapter raised while being built or drained -- such a source also appears in `sources`
    with all-zero stats, so an empty source and a failed one are never confused."""

    pages_attempted: int
    outcomes: tuple[tuple[str, int], ...]
    indexed: int
    hosts_stopped: tuple[str, ...]
    stopped_reason: str
    checkpoints_written: int
    sources: tuple[tuple[str, AdapterStats], ...] | None
    source_index: SourceIndexReport | None
    source_errors: tuple[tuple[str, str], ...] = ()


# ------------------------------------------------------------------------------------ sources stage


def _adapter_stats_entry(name: str, adapter: object) -> tuple[str, AdapterStats]:
    stats = adapter.stats()  # type: ignore[attr-defined]
    if not isinstance(stats, AdapterStats):
        raise TypeError(f"{name}.stats() must return AdapterStats")
    return name, stats


def _fetch_github_tree(fetch: Fetcher, tree_url: str) -> FetchResponse:
    """One bounded GET of a GitHub git-tree listing. The default `urllib_fetch` sends
    `Accept: text/html`, which api.github.com refuses with 415, so the default fetcher is called
    with the GitHub JSON media type; an injected fetcher (tests) is called with the plain
    four-argument `Fetcher` shape and must serve JSON on its own."""
    if fetch is urllib_fetch:
        return urllib_fetch(
            tree_url, _TREE_TIMEOUT_S, _TREE_MAX_BYTES, USER_AGENT, accept=_GITHUB_JSON_ACCEPT
        )
    return fetch(tree_url, _TREE_TIMEOUT_S, _TREE_MAX_BYTES, USER_AGENT)


def _run_sources_stage(
    config: CrawlConfig,
    *,
    root: Path,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    fetch: Fetcher,
    clock: Callable[[], float],
    rooms_jsonl: str | None,
    gh_runner: GhRunner | _GhRunnerDisabled | None,
) -> tuple[
    tuple[tuple[str, AdapterStats], ...], SourceIndexReport | None, tuple[tuple[str, str], ...]
]:
    stats: list[tuple[str, AdapterStats]] = []
    errors: list[tuple[str, str]] = []
    all_docs: list[SourceDoc] = []

    effective_rooms_jsonl = rooms_jsonl if rooms_jsonl is not None else config.rooms_jsonl
    if effective_rooms_jsonl:
        try:
            room_adapter = RoomDirectoryAdapter(Path(effective_rooms_jsonl))
            docs = list(room_adapter.iter_documents())
            stats.append(_adapter_stats_entry(room_adapter.name, room_adapter))
            all_docs.extend(docs)
        except Exception as exc:
            stats.append(("technocore_room_directory", AdapterStats(0, 0, 0, 0, 0)))
            errors.append(("technocore_room_directory", f"{type(exc).__name__}: {exc}"[:500]))

    for gd in config.github_docs:
        label = f"github_repo_docs:{gd.owner}/{gd.repo}"
        try:
            tree_url = (
                f"https://api.github.com/repos/{gd.owner}/{gd.repo}/git/trees/{gd.commit}"
                "?recursive=1"
            )
            answer = _fetch_github_tree(fetch, tree_url)
            if answer.status != 200:
                raise ValueError(
                    f"tree fetch for {gd.owner}/{gd.repo}@{gd.commit} returned HTTP {answer.status}"
                )
            tree_json = json.loads(answer.body.decode("utf-8"))
            paths = markdown_paths_from_tree(tree_json)
            docs_adapter = GitHubRepoDocsAdapter(
                owner=gd.owner, repo=gd.repo, commit=gd.commit, paths=paths,
                fetcher=fetch, clock=clock,
            )
            docs = list(docs_adapter.iter_documents())
            stats.append((label, docs_adapter.stats()))
            all_docs.extend(docs)
        except Exception as exc:
            stats.append((label, AdapterStats(0, 0, 0, 0, 0)))
            errors.append((label, f"{type(exc).__name__}: {exc}"[:500]))

    if config.github_issues and not isinstance(gh_runner, _GhRunnerDisabled):
        runner: GhRunner | None = gh_runner
        for gi in config.github_issues:
            label = f"github_issues:{gi.owner}/{gi.repo}"
            try:
                issues, comments = load_issues_with_gh(gi.owner, gi.repo, runner=runner)
                issues_adapter = GitHubIssuesAdapter(
                    owner=gi.owner, repo=gi.repo, issues=issues, comments_by_number=comments,
                    fetched_at=clock(),
                )
                docs = list(issues_adapter.iter_documents())
                stats.append((label, issues_adapter.stats()))
                all_docs.extend(docs)
            except Exception as exc:
                stats.append((label, AdapterStats(0, 0, 0, 0, 0)))
                errors.append((label, f"{type(exc).__name__}: {exc}"[:500]))

    if config.site_pages:
        try:
            site_adapter = SitePagesAdapter(
                urls=list(config.site_pages), fetcher=fetch,
                allowed_hosts=[h.host for h in config.hosts], kind="site", clock=clock,
            )
            docs = list(site_adapter.iter_documents())
            stats.append(_adapter_stats_entry(site_adapter.name, site_adapter))
            all_docs.extend(docs)
        except Exception as exc:
            stats.append(("flop_site_pages", AdapterStats(0, 0, 0, 0, 0)))
            errors.append(("flop_site_pages", f"{type(exc).__name__}: {exc}"[:500]))

    if not all_docs:
        return tuple(stats), None, tuple(errors)

    source_report = index_source_documents(
        all_docs, store=store, embedder=embedder, chunk_size=chunk_size, overlap=overlap, root=root,
    )
    return tuple(stats), source_report, tuple(errors)


# -------------------------------------------------------------------------------------- crawl stage


def run_crawl(
    config: CrawlConfig,
    *,
    root: Path,
    store: VectorStore,
    embedder: Embedder,
    chunk_size: int,
    overlap: int,
    resume: bool = False,
    checkpoint_every: int = 25,
    min_interval_s: float = 1.0,
    fetcher: Fetcher | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] | None = None,
    skip_sources: bool = False,
    rooms_jsonl: str | None = None,
    gh_runner: GhRunner | _GhRunnerDisabled | None = None,
    max_pages_override: int | None = None,
) -> CrawlReport:
    """Run the sources stage (unless `skip_sources`) then the bounded BFS crawl stage described in
    the module docstring, and return one `CrawlReport`. `root` is created if missing.

    Raises `CrawlResumeError` (before anything else happens: no adapter is built, nothing is
    fetched) when `resume=True` and there is no state file at `root/crawl-state.json`, the file is
    unreadable, or its `config_sha256` does not match `config_sha256(config)`. Any exception raised
    while ingesting (for example an embedder failure) still triggers one final state checkpoint
    before propagating unchanged -- the crawl stage's `try`/`finally` covers exactly that.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    config_hash = config_sha256(config)

    effective_hosts = tuple(
        HostRule(
            host=h.host,
            max_pages=max_pages_override if max_pages_override is not None else h.max_pages,
            path_prefixes=h.path_prefixes,
        )
        for h in config.hosts
    )
    rules_by_host: dict[str, HostRule] = {h.host: h for h in effective_hosts}

    if resume:
        state_path = _state_path(root)
        if not state_path.exists():
            raise CrawlResumeError(f"--resume given but no state file at {state_path}")
        try:
            prior = CrawlState.from_json(json.loads(state_path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise CrawlResumeError(
                f"crawl-state.json at {state_path} is unreadable: {exc}"
            ) from exc
        if prior.config_sha256 != config_hash:
            raise CrawlResumeError(
                "the saved crawl state was written for a different config (config_sha256 "
                f"mismatch: saved {prior.config_sha256!r}, current {config_hash!r}); refusing to "
                "resume with a different config"
            )
        frontier_seed = list(prior.frontier)
        visited: set[str] = set(prior.visited)
        fetched_by_host: dict[str, int] = dict(prior.fetched_by_host)
        pages_attempted = prior.pages_attempted
        outcomes_counter: dict[str, int] = dict(prior.outcomes)
        for h in effective_hosts:
            remaining = h.max_pages - fetched_by_host.get(h.host, 0)
            marker = root / f"STOP-{h.host}"
            if remaining > 0 and marker.exists():
                marker.unlink()
    else:
        frontier_seed = list(dict.fromkeys(config.seeds))
        visited = set()
        fetched_by_host = {}
        pages_attempted = 0
        outcomes_counter = {}

    allowlist_entries_: list[AllowlistEntry] = []
    for h in effective_hosts:
        remaining = h.max_pages - fetched_by_host.get(h.host, 0)
        if remaining > 0:
            allowlist_entries_.append(AllowlistEntry(host=h.host, max_pages=remaining))

    ingester: LiveIngester | None = None
    if allowlist_entries_:
        ingester = LiveIngester(
            root=root, allowlist=allowlist_entries_, store=store, embedder=embedder,
            chunk_size=chunk_size, overlap=overlap, min_interval_s=min_interval_s,
            fetcher=fetcher, clock=clock, sleep=sleep,
        )

    fetch: Fetcher = fetcher if fetcher is not None else urllib_fetch
    sources: tuple[tuple[str, AdapterStats], ...] | None = None
    source_index: SourceIndexReport | None = None
    source_errors: tuple[tuple[str, str], ...] = ()
    if not skip_sources:
        sources, source_index, source_errors = _run_sources_stage(
            config, root=root, store=store, embedder=embedder, chunk_size=chunk_size,
            overlap=overlap, fetch=fetch, clock=clock, rooms_jsonl=rooms_jsonl, gh_runner=gh_runner,
        )

    frontier: deque[str] = deque()
    frontier_set: set[str] = set()
    for seed in frontier_seed:
        if seed in frontier_set or seed in visited:
            continue
        if not url_allowed(seed, rules_by_host):
            continue
        frontier.append(seed)
        frontier_set.add(seed)

    checkpoints_written = 0

    def _checkpoint() -> None:
        nonlocal checkpoints_written
        state = CrawlState(
            frontier=tuple(frontier),
            visited=tuple(sorted(visited)),
            fetched_by_host=tuple(sorted(fetched_by_host.items())),
            config_sha256=config_hash,
            pages_attempted=pages_attempted,
            outcomes=tuple(sorted(outcomes_counter.items())),
        )
        _write_state_atomic(root, state)
        checkpoints_written += 1

    stopped_reason = "frontier_empty"
    # Consecutive pop/requeue cycles that made no forward progress: a URL popped and put right
    # back on the frontier because its host has no live budget left THIS run, without ever
    # reaching `ingester.ingest()`. Reset to 0 by every actual `ingest()` call. The `all(...)`
    # check above only becomes true once EVERY host in `ingester.hosts` is individually stopped
    # -- a host that still has spare budget but has simply run out of its own discoverable links
    # never reaches that state on its own. Without this counter, once the frontier holds nothing
    # but stuck URLs for an exhausted host, they would be popped and re-appended forever while
    # that other, never-stopped host sits idle: an unbounded spin with no exception, no return,
    # and no further checkpoint. Once `stall` reaches the current frontier length, a full lap has
    # passed with zero progress -- nothing left in the frontier can be ingested this run -- so
    # stop (the checkpoint below still persists those leftover URLs for a future `--resume` with
    # more budget) instead of spinning.
    stall = 0
    try:
        while True:
            if not frontier:
                stopped_reason = "frontier_empty"
                break
            if ingester is None or all(ingester.budget.stopped(h) for h in ingester.hosts):
                stopped_reason = "budget"
                break
            if stall >= len(frontier):
                stopped_reason = "budget"
                break

            url = frontier.popleft()
            frontier_set.discard(url)
            if url in visited:
                continue

            host = (urlparse(url).hostname or "").lower()
            if host not in ingester.hosts or ingester.budget.stopped(host):
                # No (or no longer any) budget for this host this run: leave it pending rather
                # than spend an attempt we already know will be refused. No progress made.
                frontier.append(url)
                frontier_set.add(url)
                stall += 1
                continue

            report = ingester.ingest(url)
            stall = 0
            pages_attempted += 1
            outcomes_counter[report.outcome] = outcomes_counter.get(report.outcome, 0) + 1
            if report.outcome not in _BUDGET_NOT_CONSUMED_OUTCOMES:
                fetched_by_host[host] = fetched_by_host.get(host, 0) + 1

            if report.outcome == "refused_budget":
                frontier.append(url)
                frontier_set.add(url)
            else:
                visited.add(url)
                if report.doc_sha256:
                    raw_path = root / "raw" / f"{report.doc_sha256}.html"
                    if raw_path.exists():
                        html_text = raw_path.read_text(encoding="utf-8", errors="replace")
                        for link in extract_links(html_text, url):
                            if link in visited or link in frontier_set:
                                continue
                            if not url_allowed(link, rules_by_host):
                                continue
                            frontier.append(link)
                            frontier_set.add(link)

            if checkpoint_every > 0 and pages_attempted % checkpoint_every == 0:
                _checkpoint()
    finally:
        _checkpoint()

    budgeted_hosts = set(ingester.hosts) if ingester is not None else set()
    excluded_hosts = {h.host for h in effective_hosts} - budgeted_hosts
    stopped_now = {h for h in budgeted_hosts if ingester is not None and ingester.budget.stopped(h)}
    hosts_stopped = tuple(sorted(excluded_hosts | stopped_now))

    return CrawlReport(
        pages_attempted=pages_attempted,
        outcomes=tuple(sorted(outcomes_counter.items())),
        indexed=outcomes_counter.get("indexed", 0),
        hosts_stopped=hosts_stopped,
        stopped_reason=stopped_reason,
        checkpoints_written=checkpoints_written,
        sources=sources,
        source_index=source_index,
        source_errors=source_errors,
    )


# --------------------------------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openagentsearch.pipeline.crawl")
    parser.add_argument("--allowlist", "--config", dest="config_path", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--embedder", required=True, choices=("keyword", "ollama"))
    parser.add_argument("--dimension", type=int, default=None)
    parser.add_argument("--max-pages-per-host", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--min-interval", type=float, default=1.0)
    parser.add_argument("--skip-sources", action="store_true")
    parser.add_argument("--rooms-jsonl", default=None)
    parser.add_argument("--gh-runner-disabled", action="store_true")
    parser.add_argument(
        "--seed", action="append", default=[],
        help=(
            "extra seed URL (repeatable); only used to seed a fresh run's frontier -- ignored on "
            "--resume, which reuses the persisted frontier and is unaffected by this flag"
        ),
    )
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="nomic-embed-text")
    return parser


def _report_to_json(report: CrawlReport) -> dict[str, object]:
    sources_json: list[list[object]] | None = None
    if report.sources is not None:
        sources_json = [[name, dataclasses.asdict(stats)] for name, stats in report.sources]

    source_index_json: dict[str, object] | None = None
    source_index = report.source_index
    if source_index is not None:
        source_index_json = {
            "indexed": source_index.indexed,
            "already_indexed": source_index.already_indexed,
            "failed": source_index.failed,
            "failures": [list(pair) for pair in source_index.failures],
        }

    return {
        "pages_attempted": report.pages_attempted,
        "outcomes": {name: count for name, count in report.outcomes},
        "indexed": report.indexed,
        "hosts_stopped": list(report.hosts_stopped),
        "stopped_reason": report.stopped_reason,
        "checkpoints_written": report.checkpoints_written,
        "sources": sources_json,
        "source_index": source_index_json,
        "source_errors": [list(pair) for pair in report.source_errors],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run one bounded crawl, print one compact JSON report line to stdout, and
    also write it to `root/crawl-report.json`. Returns 0 on a normal stop. Returns 2, with a JSON
    `{"error": "..."}` line to stderr, for `CrawlResumeError` (bad or missing `--resume` state).
    Returns 1, same error-line shape, for any other exception. Argument-parsing failures exit 2
    directly via argparse and never reach this function's return statement."""
    args = _build_parser().parse_args(argv)
    dimension = args.dimension if args.dimension is not None else _DEFAULT_DIMENSION[args.embedder]
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    try:
        config = load_crawl_config(args.config_path)
        if args.seed and not args.resume:
            # --seed only ever seeds a FRESH run's frontier: `run_crawl`'s `--resume` branch
            # seeds the frontier from the persisted `crawl-state.json` frontier, never from
            # `config.seeds` (see its resume branch above), so folding --seed into `config` on a
            # --resume invocation would change nothing about what gets crawled -- it would only
            # change `config_sha256` and spuriously refuse an otherwise-unchanged --resume
            # (`CrawlResumeError`). Keep --seed out of the config used for a --resume run, the
            # same way `--max-pages-per-host` and `--rooms-jsonl` are kept out of the fingerprint.
            config = dataclasses.replace(config, seeds=config.seeds + tuple(args.seed))
        store = VectorStore(args.db, dimension)
        try:
            embedder: Embedder
            if args.embedder == "keyword":
                embedder = KeywordEmbedder(dimension)
            else:
                embedder = OllamaEmbedClient(args.ollama_url, args.ollama_model)
            gh_runner: GhRunner | _GhRunnerDisabled | None = (
                GH_RUNNER_DISABLED if args.gh_runner_disabled else None
            )
            report = run_crawl(
                config, root=root, store=store, embedder=embedder,
                chunk_size=_DEFAULT_CHUNK_SIZE, overlap=_DEFAULT_OVERLAP,
                resume=args.resume, checkpoint_every=args.checkpoint_every,
                min_interval_s=args.min_interval, skip_sources=args.skip_sources,
                rooms_jsonl=args.rooms_jsonl, gh_runner=gh_runner,
                max_pages_override=args.max_pages_per_host,
            )
        finally:
            store.close()
    except CrawlResumeError as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), file=sys.stderr, flush=True)
        return 2
    except Exception as exc:
        print(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, separators=(",", ":")),
            file=sys.stderr, flush=True,
        )
        return 1

    payload = _report_to_json(report)
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    (root / "crawl-report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
