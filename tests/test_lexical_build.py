"""Package C2a, spec Tests items 2 and 3: `build_lexical_index`, `write_lexical_index`, and
`load_lexical_index`.

Offline and self-contained: every path comes from pytest's `tmp_path`, and the only subprocess-free
I/O is against files under it.
"""

import hashlib
from pathlib import Path

import pytest

from openagentsearch.extract.store import ExtractStore
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.index.manifest import ManifestEntry
from openagentsearch.lexical.build import LexicalSizeError, build_lexical_index, write_lexical_index
from openagentsearch.lexical.index import SCHEMA, load_lexical_index, to_json_bytes
from openagentsearch.lexical.tokenize import TOKENIZER_ID
from openagentsearch.vector.store import VectorStore

GENERATED_AT = 1_700_000_500.0


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class Fixture:
    """A VectorStore (`tests/test_publish_static.py`'s `Fixture` pattern) whose manifest holds
    four `indexed` documents across two `source_kind`s -- one whose extracted record is written
    and then deleted (simulating a missing record), one `#section` URL, a term ("common") present
    in 3 of the 4 docs (75% > the default 50% max_df_ratio, so it is dropped), and an underscore
    identifier -- plus one `failed` document, to confirm non-`indexed` rows never become docs."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.db_path = self.root / "vectors.sqlite3"
        self.store = VectorStore(self.db_path, dimension=2)
        self.raw = RawStore(self.root)
        self.extracted = ExtractStore(self.root)

        self.url_alpha = "https://docs.example.test/alpha"
        self.sha_alpha = self._add(
            self.url_alpha, "site_pages", "Alpha Guide",
            "Alpha guide with common terms repeated. common common alpha_term details.",
        )

        self.url_beta = "https://docs.example.test/beta"
        self.sha_beta = self._add(
            self.url_beta, "site_pages", "Beta Guide",
            "Beta guide referencing common again for testing purposes. common beta content.",
        )

        self.url_gamma = "https://github.com/example/repo/blob/main/docs/gamma.md#section-one"
        self.sha_gamma = self._add(
            self.url_gamma, "github_docs", "Gamma Doc",
            "Gamma doc common usage example. common gamma_term shown here.",
        )

        # -- missing-extracted doc: written normally, then the extracted record is deleted -- the
        # manifest still points at extracted_sha256, but root/extracted/<sha>.json is gone.
        self.url_missing = "https://docs.example.test/missing"
        self.sha_missing = self._add(
            self.url_missing, "site_pages", "Missing Guide", "This text will never be read.",
        )
        (self.root / "extracted" / f"{self.sha_missing}.json").unlink()

        self.sha_failed = _sha("failed-doc")
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_failed, source_url="https://docs.example.test/broken",
                status="failed", reason="RuntimeError: boom", indexed_at=1_700_000_300.0,
                chunk_count=0, extracted_sha256="", source_kind="site_pages",
            )
        )

    def _add(self, url: str, kind: str, title: str, text: str) -> str:
        # raw.put keys content by its own sha256 of the body, so use ITS return value as the
        # canonical doc_sha256 everywhere below (mirrors test_publish_static.py's Fixture).
        sha256 = self.raw.put(url, text.encode("utf-8"), 200, True, 1.0)
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=sha256, source_url=url, status="indexed", reason="",
                indexed_at=1_700_000_000.0, chunk_count=1, extracted_sha256=_sha(text),
                source_kind=kind,
            )
        )
        self.extracted.put(
            sha256, url, {"text": text, "title": title, "lang": "en"}, 1_700_000_001.0
        )
        return sha256

    def close(self) -> None:
        self.store.close()


# 2. build_lexical_index --------------------------------------------------------------------


def test_build_over_fixture_db_shapes_and_counts(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, report = build_lexical_index(store=fx.store, root=fx.root, generated_at=GENERATED_AT)
    finally:
        fx.close()

    assert index.schema == SCHEMA
    assert index.tokenizer == TOKENIZER_ID
    assert index.generated_at == "2023-11-14T22:21:40Z"
    assert index.db_sha256 == hashlib.sha256(fx.db_path.read_bytes()).hexdigest()
    assert index.k1 == 1.2 and index.b == 0.75

    # only the 4 indexed docs -- the failed one never appears.
    assert len(index.docs) == 4 == report.docs
    assert {doc.sha for doc in index.docs} == {
        fx.sha_alpha, fx.sha_beta, fx.sha_gamma, fx.sha_missing,
    }

    # docs order: (source_kind, source_url, doc_sha256), exactly like flop-surface.jsonl.
    assert [doc.sha for doc in index.docs] == [
        fx.sha_gamma, fx.sha_alpha, fx.sha_beta, fx.sha_missing,
    ]

    # the #section fragment is captured on the gamma doc, and empty on the others.
    by_sha = {doc.sha: doc for doc in index.docs}
    assert by_sha[fx.sha_gamma].section == "section-one"
    assert by_sha[fx.sha_alpha].section == ""

    # the missing-extracted doc: empty title, empty abstract, length 0, counted.
    missing_doc = by_sha[fx.sha_missing]
    assert missing_doc.title == ""
    assert missing_doc.abstract == ""
    assert missing_doc.length == 0
    assert report.missing_extracted == 1 == index.counts.missing_extracted

    # underscore identifiers are indexed whole AND split into their parts.
    assert "alpha_term" in index.terms
    assert "alpha" in index.terms
    assert "term" in index.terms
    assert "gamma_term" in index.terms

    # "common" is present in 3 of 4 docs (75% > the default 50% max_df_ratio) -> dropped.
    assert "common" not in index.terms
    assert index.counts.dropped_terms >= 1
    assert report.dropped_terms == index.counts.dropped_terms

    # postings for a kept term are sorted ascending by doc_index.
    for postings in index.terms.values():
        doc_indices = [doc_index for doc_index, _tf in postings]
        assert doc_indices == sorted(doc_indices)

    assert index.counts.postings == report.postings
    assert index.counts.terms == report.terms == len(index.terms)
    assert index.avgdl == pytest.approx(sum(d.length for d in index.docs) / 4)
    assert report.seconds >= 0.0


def test_two_builds_same_generated_at_are_byte_identical(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index1, _r1 = build_lexical_index(store=fx.store, root=fx.root, generated_at=GENERATED_AT)
        index2, _r2 = build_lexical_index(store=fx.store, root=fx.root, generated_at=GENERATED_AT)
    finally:
        fx.close()
    assert to_json_bytes(index1) == to_json_bytes(index2)


def test_zero_docs_has_zero_avgdl_and_drops_nothing(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    try:
        index, report = build_lexical_index(store=store, root=root, generated_at=GENERATED_AT)
    finally:
        store.close()
    assert index.docs == ()
    assert index.terms == {}
    assert index.avgdl == 0.0
    assert report.dropped_terms == 0
    assert report.docs == 0


def test_clock_is_used_when_generated_at_is_not_given(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    try:
        index, _report = build_lexical_index(store=store, root=root, clock=lambda: 1_700_001_000.0)
    finally:
        store.close()
    assert index.generated_at == "2023-11-14T22:30:00Z"


def test_max_df_ratio_is_configurable(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        # a permissive ratio keeps even a 75%-df term.
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT, max_df_ratio=0.9
        )
    finally:
        fx.close()
    assert "common" in index.terms


def test_abstract_chars_is_threaded_through_not_hardcoded(tmp_path: Path):
    """Regression: build_lexical_index() used to hardcode a 300-char abstract cut regardless of
    any abstract_chars a caller passed, so lexical-v1.json's abstract could silently diverge (in
    both length and content) from flop-surface.jsonl's abstract for the same document -- see
    pipeline.publish.build_static_index(), which now passes its own abstract_chars through."""
    fx = Fixture(tmp_path)
    try:
        index_default, _r1 = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
        index_custom, _r2 = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT, abstract_chars=10
        )
    finally:
        fx.close()

    by_sha_default = {doc.sha: doc.abstract for doc in index_default.docs}
    by_sha_custom = {doc.sha: doc.abstract for doc in index_custom.docs}
    saw_a_nonempty_abstract = False
    for sha, custom_abstract in by_sha_custom.items():
        assert len(custom_abstract) <= 10
        if custom_abstract:
            saw_a_nonempty_abstract = True
            assert custom_abstract != by_sha_default[sha]
    assert saw_a_nonempty_abstract


def test_oversized_document_text_is_truncated_not_raised(tmp_path: Path):
    """Regression: a single indexed document whose title+section+text exceeds the tokenizer's
    MAX_INPUT_CHARS bound used to raise a plain ValueError straight out of build_lexical_index()
    (tokenize() has no length guard before it). Now the combined indexed text is bounded
    (truncated), not refused, before tokenizing -- mirroring the same tolerance
    missing-extracted-record docs already get -- so one oversized document can never abort an
    otherwise-good build."""
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

        index, report = build_lexical_index(store=store, root=root, generated_at=GENERATED_AT)
    finally:
        store.close()

    assert report.docs == 1
    assert len(index.docs) == 1
    doc = index.docs[0]
    assert doc.sha == sha
    assert doc.length > 0  # tokenized fine once bounded, not skipped


# 3. write_lexical_index / load_lexical_index round trip and validation -------------------------


def test_write_then_load_round_trips_to_an_equal_index(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()

    out_path = tmp_path / "out" / "lexical-v1.json"
    written = write_lexical_index(index, out_path)
    assert written == out_path.stat().st_size
    assert written == len(to_json_bytes(index))

    loaded = load_lexical_index(out_path)
    assert loaded == index


def test_write_lexical_index_refuses_oversize_and_writes_nothing(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_path = out_dir / "lexical-v1.json"
    with pytest.raises(LexicalSizeError):
        write_lexical_index(index, out_path, max_bytes=10)

    assert list(out_dir.iterdir()) == []  # not even a temp file was left behind


def test_load_lexical_index_rejects_oversize_file(tmp_path: Path):
    path = tmp_path / "lexical-v1.json"
    path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        load_lexical_index(path, max_bytes=1)


def test_load_lexical_index_rejects_wrong_schema(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()
    out_path = tmp_path / "lexical-v1.json"
    write_lexical_index(index, out_path)

    text = out_path.read_text(encoding="utf-8").replace(
        '"openagentsearch.lexical-index/1"', '"something-else/1"', 1
    )
    out_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_lexical_index(out_path)


def test_load_lexical_index_rejects_wrong_tokenizer(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()
    out_path = tmp_path / "lexical-v1.json"
    write_lexical_index(index, out_path)

    text = out_path.read_text(encoding="utf-8").replace(
        '"unicode-word-casefold-v1"', '"some-other-tokenizer"', 1
    )
    out_path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="tokenizer"):
        load_lexical_index(out_path)


def test_load_lexical_index_rejects_doc_index_out_of_range(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()
    out_path = tmp_path / "lexical-v1.json"
    write_lexical_index(index, out_path)

    import json

    obj = json.loads(out_path.read_text(encoding="utf-8"))
    some_term = next(iter(obj["terms"]))
    obj["terms"][some_term] = [[999, 1]]
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="out of range"):
        load_lexical_index(out_path)


def test_load_lexical_index_rejects_unsorted_postings(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        index, _report = build_lexical_index(
            store=fx.store, root=fx.root, generated_at=GENERATED_AT
        )
    finally:
        fx.close()
    out_path = tmp_path / "lexical-v1.json"
    write_lexical_index(index, out_path)

    import json

    obj = json.loads(out_path.read_text(encoding="utf-8"))
    # find a term with at least 2 postings and reverse them
    multi_term = next(t for t, postings in obj["terms"].items() if len(postings) >= 2)
    obj["terms"][multi_term] = list(reversed(obj["terms"][multi_term]))
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="sorted"):
        load_lexical_index(out_path)


def test_load_lexical_index_rejects_non_object_top_level(tmp_path: Path):
    path = tmp_path / "lexical-v1.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        load_lexical_index(path)
