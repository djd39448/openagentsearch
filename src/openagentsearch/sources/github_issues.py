"""Adapter over already-loaded GitHub issues + comments JSON (pure, no network in the adapter
itself), plus a runtime loader that shells out to `gh` to produce that JSON.

`GitHubIssuesAdapter` never touches the network: it is handed `issues` and
`comments_by_number` that some caller already fetched (typically via `load_issues_with_gh`, but
tests pass small synthetic Python objects directly -- nothing here spawns a process).

`load_issues_with_gh` is the one piece of this module that runs `gh`, and only through an
injected `runner` -- production code passes `runner=None` to get the default
`subprocess.run(argv, shell=False, timeout=...)` behaviour, and every test injects its own
`runner` so `gh` is never actually spawned. `--paginate` makes `gh api` print each page as its own
top-level JSON array back-to-back with no separator (`[...][...]...`);
`_parse_concatenated_json_arrays` is the pure parser for that shape.

NOT guaranteed: `GitHubIssuesAdapter` trusts `fetched_at` and does not re-derive it from anything
in `issues`/`comments_by_number`, so every `SourceDoc` from one adapter instance shares one
timestamp regardless of when the underlying pages were actually fetched. `load_issues_with_gh`
does not itself skip pull requests or apply `max_doc_chars` -- that filtering lives in
`GitHubIssuesAdapter`, not here, so the raw lists this function returns still contain PR items.
"""

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Iterator

from openagentsearch.sources.base import AdapterStats, SourceDoc

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _parse_concatenated_json_arrays(data: bytes) -> list[object]:
    """Parse `gh api ... --paginate` output: one or more top-level JSON arrays concatenated with
    no separator between them (`[...][...]`), and return the concatenation of their elements as
    one list, in order. Pure; tolerates whitespace (including newlines) between arrays.

    NOT guaranteed: this does not tolerate anything other than back-to-back top-level JSON arrays
    -- NDJSON (one object per line), a top-level object, or trailing non-JSON garbage all raise
    rather than being partially recovered.
    """
    text = data.decode("utf-8")
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    items: list[object] = []
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        if not isinstance(obj, list):
            raise ValueError(
                f"expected a top-level JSON array at offset {idx}, got {type(obj).__name__}"
            )
        items.extend(obj)
        idx = end
    return items


def _require_str(mapping: Mapping[str, object], key: str) -> str:
    """`mapping[key]` as a `str`. Raises `KeyError` if absent, `TypeError` if present but not a
    string -- both are what `GitHubIssuesAdapter.iter_documents` treats as `skipped_malformed`."""
    value = mapping[key]
    if not isinstance(value, str):
        raise TypeError(f"{key!r} must be a string, got {value!r}")
    return value


def _require_int(mapping: Mapping[str, object], key: str) -> int:
    """`mapping[key]` as a non-bool `int`. Raises `KeyError`/`TypeError` like `_require_str`."""
    value = mapping[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key!r} must be an int, got {value!r}")
    return value


def _optional_str(mapping: Mapping[str, object], key: str) -> str:
    """`mapping.get(key)` as a `str`, or `""` when absent, `None`, or any non-string value."""
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _nested_login(mapping: Mapping[str, object]) -> str:
    """`mapping["user"]["login"]`. Raises `KeyError`/`TypeError` like `_require_str`."""
    user = mapping["user"]
    if not isinstance(user, Mapping):
        raise TypeError(f"'user' must be a mapping, got {user!r}")
    return _require_str(user, "login")


class GitHubIssuesAdapter:
    """`SourceAdapter` over already-loaded `gh api .../issues` + `.../issues/<n>/comments` JSON.
    `kind = "github_issue"`. Items carrying a `"pull_request"` key are skipped -- the GitHub
    issues API returns pull requests as issues too, and this adapter is issues-only."""

    name = "github_issues"
    kind = "github_issue"

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        issues: Sequence[Mapping[str, object]],
        comments_by_number: Mapping[int, Sequence[Mapping[str, object]]],
        fetched_at: float,
        max_doc_chars: int = 200_000,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.issues = list(issues)
        self.comments_by_number = dict(comments_by_number)
        self.fetched_at = fetched_at
        self.max_doc_chars = max_doc_chars
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
        for issue in self.issues:
            self._read += 1
            try:
                if not isinstance(issue, Mapping):
                    raise TypeError("issue must be a mapping")
                if "pull_request" in issue:
                    self._skipped_filtered += 1
                    continue
                number = _require_int(issue, "number")
                title = _require_str(issue, "title")
                body = _optional_str(issue, "body")
                state = _require_str(issue, "state")
                login = _nested_login(issue)
                created_at = _require_str(issue, "created_at")
                updated_at = _require_str(issue, "updated_at")
                html_url = _require_str(issue, "html_url")
                comment_count = _require_int(issue, "comments")

                raw_comments = list(self.comments_by_number.get(number, []))
                comments = sorted(raw_comments, key=lambda c: _require_str(c, "created_at"))
                comment_chunks: list[str] = []
                for comment in comments:
                    c_login = _nested_login(comment)
                    c_created = _require_str(comment, "created_at")
                    c_body = _optional_str(comment, "body")
                    comment_chunks.append(f"\n\n--- comment by {c_login} {c_created}\n{c_body}")
            except (KeyError, TypeError):
                self._skipped_malformed += 1
                continue

            content = (
                f"#{number} {title}\nstate: {state}\nauthor: {login} {created_at}\n\n{body}"
                + "".join(comment_chunks)
            )
            if len(content) > self.max_doc_chars:
                self._skipped_too_large += 1
                continue

            provenance = tuple(
                sorted(
                    {
                        "owner": self.owner,
                        "repo": self.repo,
                        "number": str(number),
                        "author": login,
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "state": state,
                        "comment_count": str(comment_count),
                        "author_kind": "github_login",
                    }.items()
                )
            )
            self._yielded += 1
            yield SourceDoc(
                url=html_url,
                kind=self.kind,
                content=content,
                content_type="text",
                fetched_at=self.fetched_at,
                provenance=provenance,
                title=f"#{number} {title}",
            )


def load_issues_with_gh(
    owner: str,
    repo: str,
    *,
    runner: Callable[[Sequence[str]], bytes] | None = None,
    timeout_s: float = 120,
) -> tuple[list[dict[str, object]], dict[int, list[dict[str, object]]]]:
    """Fetch every issue (`gh api repos/{owner}/{repo}/issues?state=all&per_page=100 --paginate`,
    PR items included -- filtering is `GitHubIssuesAdapter`'s job) and, for every issue whose
    `"comments"` count is > 0, that issue's comments
    (`gh api repos/{owner}/{repo}/issues/{n}/comments?per_page=100 --paginate`).

    `runner`, when given, replaces the default `subprocess.run([...], shell=False,
    timeout=timeout_s)` call entirely -- it receives the exact argv list (a `list[str]`, never a
    shell string) and must return the raw stdout bytes. Every test passes a `runner`; `gh` is
    never spawned by this repository's test suite.

    Only `owner`/`repo` matching `[A-Za-z0-9_.-]+` are accepted, since they are interpolated
    directly into the API path.

    NOT guaranteed: this makes one comments call per issue with comments (no batching); a repo
    with many commented issues means many calls. It does not retry a failed call.
    """
    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
        raise ValueError(f"owner and repo must match {_NAME_RE.pattern!r}: {owner!r} {repo!r}")

    def default_runner(argv: Sequence[str]) -> bytes:
        result = subprocess.run(
            list(argv), shell=False, timeout=timeout_s, capture_output=True, check=True
        )
        return result.stdout

    run = runner if runner is not None else default_runner

    issues_argv = ["gh", "api", f"repos/{owner}/{repo}/issues?state=all&per_page=100", "--paginate"]
    issues_raw = _parse_concatenated_json_arrays(run(issues_argv))
    issues = [item for item in issues_raw if isinstance(item, dict)]

    comments_by_number: dict[int, list[dict[str, object]]] = {}
    for issue in issues:
        number = issue.get("number")
        comment_count = issue.get("comments")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and isinstance(comment_count, int)
            and comment_count > 0
        ):
            comments_argv = [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{number}/comments?per_page=100",
                "--paginate",
            ]
            comments_raw = _parse_concatenated_json_arrays(run(comments_argv))
            comments_by_number[number] = [c for c in comments_raw if isinstance(c, dict)]

    return issues, comments_by_number
