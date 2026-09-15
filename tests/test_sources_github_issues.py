"""Package A3: GitHubIssuesAdapter over already-loaded synthetic issue/comment JSON (pure, no
network), plus load_issues_with_gh with an injected runner (gh is never actually spawned) and
_parse_concatenated_json_arrays."""

import json
import subprocess

import pytest

from openagentsearch.sources.github_issues import (
    GitHubIssuesAdapter,
    _parse_concatenated_json_arrays,
    load_issues_with_gh,
)

OWNER, REPO = "flop-contrib", "openagentsearch"
FETCHED_AT = 1700200000.0

ISSUE_1 = {
    "number": 1,
    "title": "First issue",
    "body": "The body of the first issue.",
    "state": "open",
    "user": {"login": "alice"},
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-02T00:00:00Z",
    "html_url": "https://github.com/flop-contrib/openagentsearch/issues/1",
    "comments": 2,
}
COMMENTS_1 = [
    {
        "user": {"login": "bob"},
        "created_at": "2024-01-01T12:00:00Z",
        "body": "First comment.",
        "html_url": "https://github.com/flop-contrib/openagentsearch/issues/1#issuecomment-1",
    },
    {
        "user": {"login": "carol"},
        "created_at": "2024-01-01T06:00:00Z",  # earlier than bob's -- must sort before it
        "body": "Second comment chronologically first.",
        "html_url": "https://github.com/flop-contrib/openagentsearch/issues/1#issuecomment-2",
    },
]
ISSUE_2_IS_A_PR = {
    "number": 2,
    "title": "A pull request masquerading as an issue",
    "body": "",
    "state": "open",
    "user": {"login": "dave"},
    "created_at": "2024-01-03T00:00:00Z",
    "updated_at": "2024-01-03T00:00:00Z",
    "html_url": "https://github.com/flop-contrib/openagentsearch/pull/2",
    "comments": 0,
    "pull_request": {"url": "https://api.github.com/repos/flop-contrib/openagentsearch/pulls/2"},
}
ISSUE_3_OVERSIZED = {
    "number": 3,
    "title": "Oversized issue",
    "body": "x" * 300,
    "state": "closed",
    "user": {"login": "erin"},
    "created_at": "2024-01-04T00:00:00Z",
    "updated_at": "2024-01-05T00:00:00Z",
    "html_url": "https://github.com/flop-contrib/openagentsearch/issues/3",
    "comments": 0,
}


def _adapter(**kwargs) -> GitHubIssuesAdapter:
    return GitHubIssuesAdapter(
        owner=OWNER,
        repo=REPO,
        issues=[ISSUE_1, ISSUE_2_IS_A_PR, ISSUE_3_OVERSIZED],
        comments_by_number={1: COMMENTS_1},
        fetched_at=FETCHED_AT,
        **kwargs,
    )


# 4. Three-issue fixture: one PR (skipped), one with 2 comments, one oversized (skipped) ------


def test_pull_requests_are_skipped_oversized_is_skipped_comments_included():
    adapter = _adapter(max_doc_chars=250)  # issue 1's content is 228 chars; issue 3's is bigger
    docs = list(adapter.iter_documents())
    assert [d.url for d in docs] == ["https://github.com/flop-contrib/openagentsearch/issues/1"]
    stats = adapter.stats()
    assert stats.read == 3
    assert stats.yielded == 1
    assert stats.skipped_filtered == 1  # the PR
    assert stats.skipped_too_large == 1  # the oversized issue
    assert stats.skipped_malformed == 0


def test_content_order_and_shape():
    docs = list(_adapter().iter_documents())
    doc = docs[0]
    expected = (
        "#1 First issue\n"
        "state: open\n"
        "author: alice 2024-01-01T00:00:00Z\n\n"
        "The body of the first issue.\n\n"
        "--- comment by carol 2024-01-01T06:00:00Z\n"
        "Second comment chronologically first.\n\n"
        "--- comment by bob 2024-01-01T12:00:00Z\n"
        "First comment."
    )
    assert doc.content == expected
    assert doc.title == "#1 First issue"
    assert doc.content_type == "text"
    assert doc.kind == "github_issue"


def test_provenance_labels_author_kind_as_github_login():
    docs = list(_adapter().iter_documents())
    prov = docs[0].provenance_dict()
    assert prov["author_kind"] == "github_login"
    assert prov["author"] == "alice"
    assert prov["owner"] == OWNER
    assert prov["repo"] == REPO
    assert prov["number"] == "1"
    assert prov["state"] == "open"
    assert prov["comment_count"] == "2"
    assert prov["created_at"] == "2024-01-01T00:00:00Z"
    assert prov["updated_at"] == "2024-01-02T00:00:00Z"


def test_missing_required_field_is_skipped_malformed():
    broken = dict(ISSUE_1)
    del broken["state"]
    adapter = GitHubIssuesAdapter(
        owner=OWNER, repo=REPO, issues=[broken], comments_by_number={}, fetched_at=FETCHED_AT,
    )
    docs = list(adapter.iter_documents())
    assert docs == []
    assert adapter.stats().skipped_malformed == 1


# _parse_concatenated_json_arrays --------------------------------------------------------------


def test_parse_concatenated_json_arrays_handles_back_to_back_pages():
    data = (json.dumps([1, 2]) + json.dumps([3]) + "  \n" + json.dumps([])).encode("utf-8")
    assert _parse_concatenated_json_arrays(data) == [1, 2, 3]


def test_parse_concatenated_json_arrays_rejects_a_non_array_page():
    with pytest.raises(ValueError):
        _parse_concatenated_json_arrays(json.dumps({"not": "an array"}).encode("utf-8"))


# load_issues_with_gh: injected runner records exact argv, --paginate present, no shell -------


def test_load_issues_with_gh_records_exact_argv_and_never_touches_gh():
    calls: list[list[str]] = []

    def fake_runner(argv):
        assert isinstance(argv, list)  # a real list of tokens, never a shell command string
        calls.append(list(argv))
        if argv[2].endswith("/issues?state=all&per_page=100"):
            return json.dumps([ISSUE_1, ISSUE_3_OVERSIZED]).encode("utf-8")
        assert argv[2].endswith(f"/issues/{ISSUE_1['number']}/comments?per_page=100")
        return json.dumps(COMMENTS_1).encode("utf-8")

    issues, comments_by_number = load_issues_with_gh(OWNER, REPO, runner=fake_runner)

    assert issues == [ISSUE_1, ISSUE_3_OVERSIZED]
    assert comments_by_number == {1: COMMENTS_1}  # only issue 1 has comments > 0

    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "gh" and argv[1] == "api"
        assert argv[-1] == "--paginate"
    assert calls[0][2] == f"repos/{OWNER}/{REPO}/issues?state=all&per_page=100"
    assert calls[1][2] == f"repos/{OWNER}/{REPO}/issues/1/comments?per_page=100"


def test_load_issues_with_gh_rejects_a_bad_owner_or_repo():
    with pytest.raises(ValueError):
        load_issues_with_gh("bad owner", REPO, runner=lambda argv: b"[]")
    with pytest.raises(ValueError):
        load_issues_with_gh(OWNER, "bad/repo", runner=lambda argv: b"[]")


def test_default_runner_calls_subprocess_run_without_a_shell(monkeypatch):
    """Exercises the real (runner=None) code path without spawning a process: subprocess.run
    itself is monkeypatched, so `gh` is never actually invoked."""
    recorded = {}

    class _FakeCompleted:
        stdout = b"[]"

    def fake_run(argv, *, shell, timeout, capture_output, check):
        recorded["argv"] = argv
        recorded["shell"] = shell
        recorded["timeout"] = timeout
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    issues, comments_by_number = load_issues_with_gh(OWNER, REPO, timeout_s=42)
    assert issues == []
    assert comments_by_number == {}
    assert recorded["shell"] is False
    assert recorded["timeout"] == 42
    assert isinstance(recorded["argv"], list)
