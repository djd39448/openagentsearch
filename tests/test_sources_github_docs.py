"""Package A3: GitHubRepoDocsAdapter over a fake fetcher serving small synthetic fixture files.
No network -- the fetcher is a fake that records calls and returns canned FetchResponses."""

from pathlib import Path

import pytest

from openagentsearch.pipeline.ingest import FetchResponse
from openagentsearch.sources.base import slugify_heading
from openagentsearch.sources.github_docs import GitHubRepoDocsAdapter, markdown_paths_from_tree

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a3"
OWNER, REPO = "flop-contrib", "openagentsearch"
COMMIT = "a" * 40
MD_PATH = "docs/example.md"
TXT_PATH = "notes.txt"


def _raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{COMMIT}/{path}"


def _blob_url(path: str) -> str:
    return f"https://github.com/{OWNER}/{REPO}/blob/{COMMIT}/{path}"


class _FakeFetcher:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(
        self, url: str, timeout_s: float, max_bytes: int, user_agent: str
    ) -> FetchResponse:
        self.calls.append(url)
        return self.responses[url]


def _text_response(text: str) -> FetchResponse:
    return FetchResponse(200, "text/plain; charset=utf-8", text.encode("utf-8"), False)


@pytest.fixture
def md_text() -> str:
    return (FIXTURES / "github_repo_doc.md").read_text(encoding="utf-8")


@pytest.fixture
def txt_text() -> str:
    return (FIXTURES / "github_repo_notes.txt").read_text(encoding="utf-8")


def _make_adapter(md_text: str, txt_text: str, extra_paths: list[str] | None = None):
    responses = {
        _raw_url(MD_PATH): _text_response(md_text),
        _raw_url(TXT_PATH): _text_response(txt_text),
        _raw_url("missing.md"): FetchResponse(404, "text/plain", b"not found", False),
        _raw_url("huge.md"): FetchResponse(200, "text/plain", b"x" * 10, True),
    }
    fetcher = _FakeFetcher(responses)
    paths = [MD_PATH, TXT_PATH, "missing.md", "huge.md"]
    if extra_paths:
        paths += extra_paths
    adapter = GitHubRepoDocsAdapter(
        owner=OWNER, repo=REPO, commit=COMMIT, paths=paths, fetcher=fetcher,
    )
    return adapter, fetcher


# 3. Markdown split into section docs, .txt unsplit, non-200/truncated counted not raised -----


def test_markdown_sections_carry_slug_urls_section_title_and_commit(md_text, txt_text):
    adapter, fetcher = _make_adapter(md_text, txt_text)
    docs = list(adapter.iter_documents())

    md_docs = [d for d in docs if d.url.startswith(_blob_url(MD_PATH))]
    assert len(md_docs) == 4  # intro (no heading) + Section One/Two/Three
    for doc in md_docs:
        assert doc.kind == "github_doc"
        assert doc.content_type == "text"
        assert doc.title == "Repo Docs Title"
        assert doc.provenance_dict()["commit"] == COMMIT
        assert doc.provenance_dict()["owner"] == OWNER
        assert doc.provenance_dict()["repo"] == REPO
        assert doc.provenance_dict()["path"] == MD_PATH
        if doc.section is not None:
            assert doc.url == _blob_url(MD_PATH) + "#" + slugify_heading(doc.section)
        else:
            assert doc.url == _blob_url(MD_PATH)

    headings = {d.section for d in md_docs}
    assert headings == {None, "Section One", "Section Two", "Section Three"}
    # the fenced "## not a heading" line must never become its own section
    assert "not a heading" not in {d.section for d in md_docs}
    section_one = next(d for d in md_docs if d.section == "Section One")
    assert "## not a heading" in section_one.content


def test_txt_file_becomes_exactly_one_unsplit_doc(md_text, txt_text):
    adapter, fetcher = _make_adapter(md_text, txt_text)
    docs = list(adapter.iter_documents())
    txt_docs = [d for d in docs if d.url == _blob_url(TXT_PATH)]
    assert len(txt_docs) == 1
    assert txt_docs[0].content == txt_text
    assert txt_docs[0].section is None
    assert txt_docs[0].provenance_dict()["path"] == TXT_PATH


def test_non_200_and_truncated_paths_are_counted_not_raised(md_text, txt_text):
    adapter, fetcher = _make_adapter(md_text, txt_text)
    docs = list(adapter.iter_documents())  # must not raise
    stats = adapter.stats()
    assert stats.read == 4
    assert stats.skipped_malformed == 1  # missing.md (404)
    assert stats.skipped_too_large == 1  # huge.md (truncated)
    assert stats.yielded == len(docs)
    assert adapter.fetch_statuses[MD_PATH] == 200
    assert adapter.fetch_statuses["missing.md"] == 404
    assert adapter.fetch_statuses["huge.md"] == 200
    # every requested path was actually fetched exactly once, in order
    assert fetcher.calls == [
        _raw_url(MD_PATH), _raw_url(TXT_PATH), _raw_url("missing.md"), _raw_url("huge.md"),
    ]


def test_markdown_starting_directly_at_a_heading_has_no_empty_leading_doc():
    # A file with no preamble text produces a leading (None, "") part from
    # split_markdown_sections(); it must be dropped, not turned into an empty SourceDoc.
    md_text_no_preamble = "## Only Heading\n" + ("filler text " * 30) + "\n"
    path = "only.md"
    fetcher = _FakeFetcher({_raw_url(path): _text_response(md_text_no_preamble)})
    adapter = GitHubRepoDocsAdapter(
        owner=OWNER, repo=REPO, commit=COMMIT, paths=[path], fetcher=fetcher,
    )
    docs = list(adapter.iter_documents())  # must not raise
    assert len(docs) == 1
    assert docs[0].section == "Only Heading"
    assert docs[0].content


def test_unsupported_extension_is_filtered_not_fetched_as_a_doc(md_text, txt_text):
    responses = {
        _raw_url(MD_PATH): _text_response(md_text),
        _raw_url(TXT_PATH): _text_response(txt_text),
        _raw_url("missing.md"): FetchResponse(404, "text/plain", b"not found", False),
        _raw_url("huge.md"): FetchResponse(200, "text/plain", b"x" * 10, True),
        _raw_url("image.png"): FetchResponse(200, "image/png", b"not really binary", False),
    }
    fetcher = _FakeFetcher(responses)
    adapter = GitHubRepoDocsAdapter(
        owner=OWNER, repo=REPO, commit=COMMIT,
        paths=[MD_PATH, TXT_PATH, "missing.md", "huge.md", "image.png"], fetcher=fetcher,
    )
    docs = list(adapter.iter_documents())
    assert not any(d.provenance_dict().get("path") == "image.png" for d in docs)
    assert adapter.stats().skipped_filtered == 1


# markdown_paths_from_tree: sorts, blobs only, refuses truncated trees ------------------------


def test_markdown_paths_from_tree_sorts_and_keeps_only_matching_blobs():
    tree_json = {
        "sha": "deadbeef",
        "truncated": False,
        "tree": [
            {"path": "z.md", "type": "blob", "size": 10, "sha": "1"},
            {"path": "docs", "type": "tree", "size": 0, "sha": "2"},
            {"path": "a.md", "type": "blob", "size": 20, "sha": "3"},
            {"path": "a.md.bak", "type": "blob", "size": 5, "sha": "4"},
            {"path": "notes.txt", "type": "blob", "size": 5, "sha": "5"},
        ],
    }
    assert markdown_paths_from_tree(tree_json) == ["a.md", "z.md"]
    assert markdown_paths_from_tree(tree_json, include_suffixes=(".md", ".txt")) == [
        "a.md", "notes.txt", "z.md",
    ]


def test_markdown_paths_from_tree_refuses_a_truncated_tree():
    with pytest.raises(ValueError, match="truncated"):
        markdown_paths_from_tree({"truncated": True, "tree": []})


# constructor validation -----------------------------------------------------------------------


def test_commit_must_be_40_lowercase_hex():
    with pytest.raises(ValueError):
        GitHubRepoDocsAdapter(
            owner=OWNER, repo=REPO, commit="not-a-sha", paths=["a.md"],
            fetcher=_FakeFetcher({}),
        )


def test_path_must_be_relative_with_no_dotdot():
    for bad_path in ("/abs.md", "../escape.md", "a/../b.md"):
        with pytest.raises(ValueError):
            GitHubRepoDocsAdapter(
                owner=OWNER, repo=REPO, commit=COMMIT, paths=[bad_path],
                fetcher=_FakeFetcher({}),
            )
