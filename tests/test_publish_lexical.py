"""Package C2a, spec Deliverable item 6 / Tests item 6: `pipeline.publish`'s lexical-index
integration -- `build_static_index()` also writing `index/lexical-v1.json`, and a `LexicalSizeError`
being reported (`PublishReport.lexical_error`) rather than raised.

Self-contained, offline: every path comes from pytest's `tmp_path`.
"""

import hashlib
import json
from pathlib import Path

from openagentsearch.extract.store import ExtractStore
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.index.manifest import ManifestEntry
from openagentsearch.pipeline import publish as publish_module
from openagentsearch.pipeline.publish import PublishReport, build_static_index
from openagentsearch.vector.store import VectorStore


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class Fixture:
    """Two indexed documents of different `source_kind`s, real raw + extracted records --
    just enough for both `flop-surface.jsonl` and `lexical-v1.json` to have real content."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.db_path = self.root / "vectors.sqlite3"
        self.store = VectorStore(self.db_path, dimension=2)
        self.raw = RawStore(self.root)
        self.extracted = ExtractStore(self.root)

        self.url_a = "https://docs.example.test/guide"
        self.sha_a = self.raw.put(
            self.url_a, b"<html><body>Guide body text for lexical indexing.</body></html>",
            200, True, 1_700_000_000.0,
        )
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_a, source_url=self.url_a, status="indexed", reason="",
                indexed_at=1_700_000_000.0, chunk_count=1, extracted_sha256=_sha("a-text"),
                source_kind="site_pages",
            )
        )
        self.extracted.put(
            self.sha_a, self.url_a,
            {"text": "Guide body text for lexical indexing.", "title": "Guide", "lang": "en"},
            1_700_000_001.0,
        )

        self.url_b = "https://github.com/example/repo/blob/main/docs/reference.md#usage"
        self.sha_b = self.raw.put(
            self.url_b, b"<html><body>Reference usage details for the api.</body></html>",
            200, True, 1_700_000_100.0,
        )
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_b, source_url=self.url_b, status="indexed", reason="",
                indexed_at=1_700_000_100.0, chunk_count=1, extracted_sha256=_sha("b-text"),
                source_kind="github_docs",
            )
        )
        self.extracted.put(
            self.sha_b, self.url_b,
            {"text": "Reference usage details for the api.", "title": "Reference", "lang": "en"},
            1_700_000_101.0,
        )

    def close(self) -> None:
        self.store.close()


def test_build_static_index_also_writes_lexical_v1_json(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        out = tmp_path / "out"
        report = build_static_index(
            store=fx.store, root=fx.root, out=out, generated_at=1_700_000_500.0
        )
    finally:
        fx.close()

    assert isinstance(report, PublishReport)
    assert report.lexical_error == ""
    assert report.lexical_bytes > 0

    lexical_path = out / "index" / "lexical-v1.json"
    assert lexical_path.is_file()
    assert lexical_path.stat().st_size == report.lexical_bytes

    lexical_obj = json.loads(lexical_path.read_text(encoding="utf-8"))
    assert lexical_obj["schema"] == "openagentsearch.lexical-index/1"
    assert len(lexical_obj["docs"]) == 2
    assert {doc["sha"] for doc in lexical_obj["docs"]} == {fx.sha_a, fx.sha_b}

    # the other two files are unaffected by the addition.
    assert (out / "index" / "manifest.json").is_file()
    assert (out / "index" / "flop-surface.jsonl").is_file()

    readme = (out / "index" / "README.txt").read_text(encoding="utf-8")
    assert "lexical-v1.json" in readme


def test_lexical_size_error_is_reported_not_raised_and_other_files_survive(
    tmp_path: Path, monkeypatch
):
    fx = Fixture(tmp_path)
    monkeypatch.setattr(publish_module, "_LEXICAL_MAX_BYTES", 10)
    try:
        out = tmp_path / "out"
        report = build_static_index(
            store=fx.store, root=fx.root, out=out, generated_at=1_700_000_500.0
        )
    finally:
        fx.close()

    assert report.lexical_error != ""
    assert "LexicalSizeError" in report.lexical_error or "10 byte" in report.lexical_error
    assert report.lexical_bytes == 0

    # manifest.json and flop-surface.jsonl are intact and correct despite the lexical failure.
    assert report.documents == 2
    assert report.surface_lines == 2
    manifest_path = Path(out / "index" / "manifest.json")
    surface_path = Path(out / "index" / "flop-surface.jsonl")
    assert manifest_path.is_file()
    assert surface_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 2

    # the size guard fired before any write: no lexical-v1.json and no leftover temp file.
    leftover = sorted(p.name for p in (out / "index").iterdir())
    assert "lexical-v1.json" not in leftover
    assert not any(name.startswith(".lexical-v1.json.") for name in leftover)


def test_missing_extracted_records_do_not_break_the_lexical_build(tmp_path: Path):
    """The same 'missing extracted record' tolerance flop-surface.jsonl has (see
    test_publish_static.py) must hold for the lexical index too: build_static_index() must not
    raise just because root/extracted/<sha>.json is absent for an indexed row."""
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    try:
        store.record_manifest(
            ManifestEntry(
                doc_sha256=_sha("no-extract"), source_url="https://docs.example.test/orphan",
                status="indexed", reason="", indexed_at=1_700_000_000.0, chunk_count=1,
                extracted_sha256=_sha("orphan-text"), source_kind="site_pages",
            )
        )
        report = build_static_index(store=store, root=root, out=tmp_path / "out")
    finally:
        store.close()

    assert report.lexical_error == ""
    assert report.lexical_bytes > 0


def test_oversized_document_does_not_abort_the_whole_publish(tmp_path: Path):
    """Regression (F1): a single indexed document whose title+section+text exceeds the
    tokenizer's 1,000,000-character bound used to raise a plain ValueError straight out of
    build_static_index() -- uncaught, since only LexicalSizeError was caught around the lexical
    build -- discarding the whole PublishReport even though manifest.json and flop-surface.jsonl
    were already durable on disk. The oversized text is now bounded (truncated), not refused,
    inside build_lexical_index(), so the publish succeeds and all three files are written."""
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    raw = RawStore(root)
    extracted = ExtractStore(root)
    try:
        big_text = "word " * 210_000  # ~1.05M chars, over the tokenizer's 1,000,000-char limit
        url = "https://docs.example.test/huge"
        sha = raw.put(url, big_text.encode("utf-8"), 200, True, 1.0)
        store.record_manifest(
            ManifestEntry(
                doc_sha256=sha, source_url=url, status="indexed", reason="",
                indexed_at=1_700_000_000.0, chunk_count=1, extracted_sha256=_sha(big_text),
                source_kind="site_pages",
            )
        )
        extracted.put(sha, url, {"text": big_text, "title": "Huge", "lang": "en"}, 1.0)

        out = tmp_path / "out"
        report = build_static_index(
            store=store, root=root, out=out, generated_at=1_700_000_500.0
        )
    finally:
        store.close()

    assert isinstance(report, PublishReport)
    assert report.lexical_error == ""
    assert report.lexical_bytes > 0
    assert (out / "index" / "manifest.json").is_file()
    assert (out / "index" / "flop-surface.jsonl").is_file()
    assert (out / "index" / "lexical-v1.json").is_file()


def test_lexical_abstract_matches_surface_abstract_for_a_nondefault_abstract_chars(
    tmp_path: Path,
):
    """Regression (F2): build_lexical_index() used to hardcode its abstract cut at 300 chars
    regardless of the abstract_chars build_static_index() was actually called with (exposed via
    the public --abstract-chars CLI flag), so lexical-v1.json's abstract for a document could
    silently diverge in both length and content from flop-surface.jsonl's abstract for the SAME
    document. They must always be the identical lexical cut."""
    fx = Fixture(tmp_path)
    try:
        out = tmp_path / "out"
        report = build_static_index(
            store=fx.store, root=fx.root, out=out, generated_at=1_700_000_500.0,
            abstract_chars=10,
        )
    finally:
        fx.close()

    assert report.lexical_error == ""
    surface_abstract_by_sha = {}
    surface_text = (out / "index" / "flop-surface.jsonl").read_text(encoding="utf-8")
    for line in surface_text.splitlines():
        row = json.loads(line)
        surface_abstract_by_sha[row["doc_sha256"]] = row["abstract"]

    lexical_obj = json.loads((out / "index" / "lexical-v1.json").read_text(encoding="utf-8"))
    assert lexical_obj["docs"]  # sanity: the fixture actually produced docs
    for doc in lexical_obj["docs"]:
        assert doc["abstract"] == surface_abstract_by_sha[doc["sha"]]
        assert len(doc["abstract"]) <= 10


def test_lexical_index_reuses_the_manifest_read_and_db_hash_publish_already_did(
    tmp_path: Path, monkeypatch
):
    """Regression (F3): build_lexical_index() used to perform its OWN independent
    store.manifest_entries() call and its own independent DB-file hash, rather than reusing the
    ones build_static_index() already read/computed for manifest.json -- so a manifest row
    written between the two reads (VectorStore is explicitly safe for concurrent access) could
    make lexical-v1.json's doc set/db_sha256 disagree with manifest.json's, contradicting
    docs/static-index.md's 'always in agreement ... built from the same manifest read' claim.
    Now build_static_index() passes its own already-read entries/db_sha256 through, so
    store.manifest_entries() is called exactly once for the whole export."""
    fx = Fixture(tmp_path)
    entries_calls = {"n": 0}
    original_entries = VectorStore.manifest_entries

    def counting_entries(self):
        entries_calls["n"] += 1
        return original_entries(self)

    monkeypatch.setattr(VectorStore, "manifest_entries", counting_entries)
    try:
        out = tmp_path / "out"
        report = build_static_index(
            store=fx.store, root=fx.root, out=out, generated_at=1_700_000_500.0
        )
    finally:
        fx.close()

    assert entries_calls["n"] == 1, (
        "build_static_index() must read store.manifest_entries() exactly once and pass those "
        "same entries into build_lexical_index(), not have it perform its own independent "
        "second read"
    )
    assert report.lexical_error == ""

    manifest = json.loads((out / "index" / "manifest.json").read_text(encoding="utf-8"))
    lexical = json.loads((out / "index" / "lexical-v1.json").read_text(encoding="utf-8"))
    assert manifest["db_sha256"] == lexical["db_sha256"] == report.db_sha256
    assert len(manifest["documents"]) == len(lexical["docs"]) == 2
