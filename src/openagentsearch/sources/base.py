"""Shared types and pure helpers used by every `openagentsearch.sources.*` adapter.

`SourceDoc` is the one document shape every adapter yields; `AdapterStats` is the one accounting
shape every adapter reports; `SourceAdapter` is the structural contract (`Protocol`, not a base
class -- an adapter satisfies it by shape, not by inheritance). `split_markdown_sections` and
`slugify_heading` are pure text-processing helpers with no I/O, shared by the GitHub docs adapter
and available to any future markdown-shaped source.
"""

import math
import re
from dataclasses import dataclass
from typing import Iterator, Protocol

_KIND_RE = re.compile(r"^[a-z_]+$")
_CONTENT_TYPES = ("html", "text")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_HEADING_PREFIXES = ("## ", "### ")
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SourceDoc:
    """One document produced by a source adapter, ready for
    `openagentsearch.pipeline.index.index_source_document`.

    `provenance` is a tuple of `(key, value)` string pairs -- adapters build it pre-sorted by key
    for determinism, but that is a convention of this module's adapters, not something
    `__post_init__` enforces or that a caller may rely on for a `SourceDoc` built elsewhere.
    `provenance_dict()` is the only supported way to look values up by key.

    NOT guaranteed: `__post_init__` validates shape (non-empty `url`, `kind` matching `[a-z_]+`,
    `content_type` in `{"html", "text"}`, non-empty `content`, a finite `fetched_at`, and unique
    provenance keys) but never validates that `url` is a real, reachable, or even well-formed URL,
    that `content` is well-formed HTML/text for its declared `content_type`, or that `provenance`
    values are truthful -- adapters are trusted to have populated them correctly from what they
    actually saw.
    """

    url: str
    kind: str
    content: str
    content_type: str
    fetched_at: float
    provenance: tuple[tuple[str, str], ...]
    title: str | None = None
    section: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(self.kind, str) or not _KIND_RE.match(self.kind):
            raise ValueError(f"kind must match {_KIND_RE.pattern!r}, got {self.kind!r}")
        if self.content_type not in _CONTENT_TYPES:
            raise ValueError(
                f"content_type must be one of {_CONTENT_TYPES}, got {self.content_type!r}"
            )
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("content must be a non-empty string")
        if isinstance(self.fetched_at, bool) or not isinstance(self.fetched_at, (int, float)):
            raise ValueError(f"fetched_at must be a number, got {self.fetched_at!r}")
        if not math.isfinite(self.fetched_at):
            raise ValueError(f"fetched_at must be finite, got {self.fetched_at!r}")
        object.__setattr__(self, "fetched_at", float(self.fetched_at))
        if not isinstance(self.provenance, tuple):
            raise ValueError("provenance must be a tuple of (str, str) pairs")
        keys: list[str] = []
        for pair in self.provenance:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
            ):
                raise ValueError(f"each provenance entry must be a (str, str) pair, got {pair!r}")
            keys.append(pair[0])
        if len(keys) != len(set(keys)):
            raise ValueError(f"provenance keys must be unique, got {keys!r}")

    def provenance_dict(self) -> dict[str, str]:
        """`dict(self.provenance)`. NOT guaranteed: key insertion order in the returned dict
        follows `self.provenance`'s order, not necessarily sorted, even though this module's own
        adapters happen to build sorted tuples."""
        return dict(self.provenance)


@dataclass(frozen=True)
class AdapterStats:
    """Counts of what one adapter run did with its input records. `read` counts records the
    adapter actually looked at (a blank/absent line in a line-oriented source is not a record and
    is not counted); `yielded` counts `SourceDoc`s actually produced; the three `skipped_*`
    counters classify everything else. NOT guaranteed: `read == yielded + sum(skipped_*)` for
    every adapter -- an adapter that produces more than one `SourceDoc` per input record (the
    GitHub markdown splitter) will have `yielded > read` for the records that split into more than
    one section."""

    read: int
    yielded: int
    skipped_malformed: int
    skipped_filtered: int
    skipped_too_large: int


class SourceAdapter(Protocol):
    """Structural contract every adapter in this package satisfies. NOT guaranteed: nothing here
    requires an adapter to be re-iterable -- `iter_documents()` may be a one-shot generator, and
    `stats()` may only be meaningful after it has been fully consumed. This is a `Protocol`, not a
    base class; conformance is checked by shape (mypy structural typing), not by inheritance."""

    name: str
    kind: str

    def iter_documents(self) -> Iterator[SourceDoc]: ...

    def stats(self) -> AdapterStats: ...


def split_markdown_sections(
    text: str, *, max_section_chars: int = 20_000, min_section_chars: int = 200
) -> list[tuple[str | None, str]]:
    """Split markdown `text` on lines starting with `## ` or `### ` (a `#### ` or deeper heading,
    or any of those prefixes appearing inside a fenced code block, is never a split point).

    Each returned part is `(heading_text_or_None, body_including_heading_line)`; the first part
    (before any heading) always has `heading = None`. A part shorter than `min_section_chars`
    (measured on its own, pre-merge, body length) is merged into the immediately preceding part --
    except the very first part, which has nothing to merge into and is kept regardless of length.
    A part longer than `max_section_chars` (measured post-merge) is cut at the last newline before
    the limit into two or more consecutive parts sharing the same heading; a part with no newline
    before the limit is cut with a hard character break instead.

    Fenced code blocks are recognised by a line whose stripped text starts with three or more
    backticks or tildes; any such line toggles fenced-block state, matching backtick/tilde fences
    that use a different marker or length is not distinguished from the same-family opening fence.

    Deterministic (same input and parameters always produce the same output) and lossless: the
    concatenation of every returned body equals `text` exactly (`"".join(body for _, body in
    result) == text`), including all original line endings and any trailing partial line.

    NOT guaranteed: heading text is not deduplicated or validated for uniqueness across parts (two
    identical headings produce two parts with the same heading text); heading levels deeper than
    `###` are never split points and stay embedded in whatever part contains them; a hard cut
    (no newline available before `max_section_chars`) can split a multi-byte character's
    surrounding words or a fenced code block awkwardly -- it is a last-resort byte-preserving cut,
    not a markdown-aware one.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    lines = text.splitlines(keepends=True)
    raw: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    buf: list[str] = []
    in_fence = False
    for line in lines:
        bare = line.rstrip("\r\n")
        if _FENCE_RE.match(bare.strip()):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence:
            prefix = next((p for p in _HEADING_PREFIXES if bare.startswith(p)), None)
            if prefix is not None:
                raw.append((heading, buf))
                heading = bare[len(prefix) :].strip()
                buf = [line]
                continue
        buf.append(line)
    raw.append((heading, buf))

    merged: list[tuple[str | None, list[str]]] = []
    for h, lns in raw:
        body_len = sum(len(ln) for ln in lns)
        if merged and body_len < min_section_chars:
            prev_h, prev_lns = merged[-1]
            merged[-1] = (prev_h, prev_lns + lns)
        else:
            merged.append((h, lns))

    result: list[tuple[str | None, str]] = []
    for h, lns in merged:
        body = "".join(lns)
        if len(body) <= max_section_chars:
            result.append((h, body))
            continue
        remaining = body
        while len(remaining) > max_section_chars:
            cut = remaining.rfind("\n", 0, max_section_chars)
            cut = max_section_chars if cut == -1 else cut + 1
            result.append((h, remaining[:cut]))
            remaining = remaining[cut:]
        if remaining:
            result.append((h, remaining))
    return result


def slugify_heading(heading: str) -> str:
    """GitHub-style anchor slug for one heading: lowercase, punctuation other than `-`/`_`
    stripped, runs of whitespace collapsed to a single `-`. Pure.

    NOT guaranteed: this does not reproduce GitHub's full anchor algorithm -- it does not
    de-duplicate repeated slugs on a page (`heading-1`, `heading-2`, ...), does not strip leading
    or trailing hyphens, and does not special-case emoji or non-Latin scripts beyond what `\\w`
    matches for the running Python's Unicode database.
    """
    if not isinstance(heading, str):
        raise ValueError("heading must be a string")
    lowered = heading.strip().lower()
    stripped = _SLUG_STRIP_RE.sub("", lowered)
    return _SLUG_SPACE_RE.sub("-", stripped)
