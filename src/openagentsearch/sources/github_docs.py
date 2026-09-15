"""Adapter that reads a fixed set of files out of one pinned GitHub commit, through
raw.githubusercontent.com, via an injected fetcher (this module opens no socket itself).

Markdown files are split into sections with `split_markdown_sections`, one `SourceDoc` per
section; other supported text files (`.txt`, `.json`) become one `SourceDoc` each, unsplit.

NOT guaranteed: this adapter never lists a repository's files on its own (see
`markdown_paths_from_tree` for that, given an already-fetched git-tree JSON) -- `paths` is always
supplied by the caller. It does not follow any redirect (the injected `fetcher` is expected to
behave like `openagentsearch.pipeline.ingest.urllib_fetch`, which never does). A transport
exception from the fetcher is treated the same as a non-200 response (`skipped_malformed`); it is
not distinguished in `AdapterStats`, only in `fetch_statuses`, which records the last HTTP status
(or omits the path) actually reached per path.
"""

import hashlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Iterator

from openagentsearch.pipeline.ingest import USER_AGENT, Fetcher
from openagentsearch.sources.base import (
    AdapterStats,
    SourceDoc,
    slugify_heading,
    split_markdown_sections,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MARKDOWN_SUFFIX = ".md"
_PLAIN_TEXT_SUFFIXES = (".txt", ".json")


def _validate_relative_path(path: str) -> None:
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ValueError(f"path must be a non-empty relative path, got {path!r}")
    if any(segment in ("", "..") for segment in path.split("/")):
        raise ValueError(f"path must not contain '..' or empty segments, got {path!r}")


def markdown_paths_from_tree(
    tree_json: Mapping[str, object], *, include_suffixes: tuple[str, ...] = (".md",)
) -> list[str]:
    """Pure: blob paths from a GitHub git-tree API payload (`{"sha", "tree": [...], "truncated"}`)
    whose path ends with one of `include_suffixes`, sorted ascending.

    Refuses (`ValueError`) a tree with `"truncated": true` -- GitHub sets that when the listing
    was too large for one response, so a truncated tree is never treated as a complete listing.

    NOT guaranteed: this does not fetch or validate that the tree corresponds to any particular
    commit; it only reshapes the JSON it is given. A malformed entry (missing `"path"` or
    `"type"`, or a non-dict entry) is skipped rather than raising.
    """
    if tree_json.get("truncated"):
        raise ValueError("tree is truncated: a partial listing must never be treated as complete")
    tree = tree_json.get("tree")
    if not isinstance(tree, list):
        raise ValueError("tree_json['tree'] must be a list")
    paths: list[str] = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.endswith(include_suffixes):
            paths.append(path)
    return sorted(paths)


def _first_h1_or_none(text: str) -> str | None:
    """First line starting with `# ` (a single-hash heading), title-cased as written, or `None`.
    NOT guaranteed: does not skip a `# ` line that happens to be inside a fenced code block."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


class GitHubRepoDocsAdapter:
    """`SourceAdapter` over a fixed list of paths at one pinned commit of one GitHub repository.
    `kind = "github_doc"`."""

    name = "github_repo_docs"
    kind = "github_doc"

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        commit: str,
        paths: Sequence[str],
        fetcher: Fetcher,
        timeout_s: float = 10.0,
        max_bytes: int = 2_000_000,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not owner or not repo:
            raise ValueError("owner and repo must be non-empty strings")
        if not _COMMIT_RE.match(commit):
            raise ValueError(f"commit must be 40 lowercase hex characters, got {commit!r}")
        for path in paths:
            _validate_relative_path(path)
        self.owner = owner
        self.repo = repo
        self.commit = commit
        self.paths = list(paths)
        self.fetcher = fetcher
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.clock = clock
        self.fetch_statuses: dict[str, int] = {}
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

    def _raw_url(self, path: str) -> str:
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.commit}/{path}"

    def _blob_url(self, path: str) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/blob/{self.commit}/{path}"

    def iter_documents(self) -> Iterator[SourceDoc]:
        for path in self.paths:
            self._read += 1
            raw_url = self._raw_url(path)
            fetched_at = self.clock()
            try:
                answer = self.fetcher(raw_url, self.timeout_s, self.max_bytes, self.user_agent)
            except Exception:
                self._skipped_malformed += 1
                continue
            self.fetch_statuses[path] = answer.status
            if answer.status != 200:
                self._skipped_malformed += 1
                continue
            if answer.truncated:
                self._skipped_too_large += 1
                continue
            try:
                text = answer.body.decode("utf-8")
            except UnicodeDecodeError:
                self._skipped_malformed += 1
                continue
            if not text:
                # an empty file has no content SourceDoc would accept; nothing useful to index
                self._skipped_malformed += 1
                continue

            file_sha256 = hashlib.sha256(answer.body).hexdigest()
            blob_url = self._blob_url(path)

            if path.endswith(_MARKDOWN_SUFFIX):
                file_title = _first_h1_or_none(text)
                title = file_title if file_title is not None else path
                for index, (heading, body) in enumerate(split_markdown_sections(text)):
                    if not body:
                        # e.g. the file's leading (heading-less) part when it starts right at a
                        # heading with no preamble text; nothing for a SourceDoc to carry.
                        continue
                    url = blob_url + (f"#{slugify_heading(heading)}" if heading else "")
                    provenance = tuple(
                        sorted(
                            {
                                "owner": self.owner,
                                "repo": self.repo,
                                "commit": self.commit,
                                "path": path,
                                "raw_url": raw_url,
                                "fetched_at": str(fetched_at),
                                "sha256": file_sha256,
                                "section_index": str(index),
                                "status": str(answer.status),
                            }.items()
                        )
                    )
                    self._yielded += 1
                    yield SourceDoc(
                        url=url,
                        kind=self.kind,
                        content=body,
                        content_type="text",
                        fetched_at=fetched_at,
                        provenance=provenance,
                        title=title,
                        section=heading,
                    )
            elif path.endswith(_PLAIN_TEXT_SUFFIXES):
                provenance = tuple(
                    sorted(
                        {
                            "owner": self.owner,
                            "repo": self.repo,
                            "commit": self.commit,
                            "path": path,
                            "raw_url": raw_url,
                            "fetched_at": str(fetched_at),
                            "sha256": file_sha256,
                            "section_index": "0",
                            "status": str(answer.status),
                        }.items()
                    )
                )
                self._yielded += 1
                yield SourceDoc(
                    url=blob_url,
                    kind=self.kind,
                    content=text,
                    content_type="text",
                    fetched_at=fetched_at,
                    provenance=provenance,
                    title=path,
                )
            else:
                self._skipped_filtered += 1
