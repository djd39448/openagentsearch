"""Package A5 (code half): the static index export (`openagentsearch.pipeline.publish`).

Everything here is offline and self-contained: no network, no machine-specific paths (every path
comes from pytest's `tmp_path`), and every subprocess call carries a timeout so a bug here can
never hang the suite.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from openagentsearch.extract.store import ExtractStore
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.index.manifest import ManifestCorruptionError, ManifestEntry
from openagentsearch.pipeline.publish import PublishReport, build_static_index
from openagentsearch.vector.store import VectorStore

REPO = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT = 60


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return env


class Fixture:
    """A VectorStore whose manifest holds: two currently-`indexed` documents of different
    `source_kind`s (one with a `#section` URL), one `superseded` document (the earlier of two
    `indexed` writes to the same URL -- superseding is `write_manifest_entry`'s own behaviour, not
    anything faked here), and one `failed` document. `extracted/` records exist, written through
    `ExtractStore` after a `RawStore.put`, for exactly the two documents that end up `indexed`."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.db_path = self.root / "vectors.sqlite3"
        self.store = VectorStore(self.db_path, dimension=2)
        self.raw = RawStore(self.root)
        self.extracted = ExtractStore(self.root)

        # -- superseded pair: url_a indexed twice; the first write is superseded by the second --
        self.url_a = "https://docs.example.test/guide"
        self.sha_superseded = self.raw.put(
            self.url_a, b"<html><body>Guide version one, now stale.</body></html>",
            200, True, 1_700_000_000.0,
        )
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_superseded, source_url=self.url_a, status="indexed", reason="",
                indexed_at=1_700_000_000.0, chunk_count=3, extracted_sha256=_sha("v1-text"),
                source_kind="site_pages",
            )
        )
        self.sha_b = self.raw.put(
            self.url_a, b"<html><body>Guide version two, replacing version one.</body></html>",
            200, True, 1_700_000_100.0,
        )
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_b, source_url=self.url_a, status="indexed", reason="",
                indexed_at=1_700_000_100.0, chunk_count=4, extracted_sha256=_sha("v2-text"),
                source_kind="site_pages",
            )
        )
        self.extracted.put(
            self.sha_b, self.url_a,
            {
                "text": "Guide version two body text for the abstract to chew on.",
                "title": "Guide v2", "lang": "en",
            },
            1_700_000_101.0,
        )

        # -- second indexed document, a different kind, with a #section URL --
        self.url_c = "https://github.com/example/repo/blob/main/docs/guide.md#getting-started"
        self.sha_c = self.raw.put(
            self.url_c,
            b"# Getting started\n\nSection body for the github docs adapter test fixture.",
            200, True, 1_700_000_200.0,
        )
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_c, source_url=self.url_c, status="indexed", reason="",
                indexed_at=1_700_000_200.0, chunk_count=2, extracted_sha256=_sha("c-text"),
                source_kind="github_docs",
            )
        )
        self.long_text = "Getting started with the project. " * 20  # 700 chars: forces truncation
        self.extracted.put(
            self.sha_c, self.url_c,
            {"text": self.long_text, "title": "Getting Started", "lang": "en"},
            1_700_000_201.0,
        )

        # -- failed document: no raw/extracted records at all, same as a real failed attempt --
        self.sha_failed = _sha("failed-doc")
        self.store.record_manifest(
            ManifestEntry(
                doc_sha256=self.sha_failed, source_url="https://docs.example.test/broken",
                status="failed", reason="RuntimeError: boom", indexed_at=1_700_000_300.0,
                chunk_count=0, extracted_sha256="", source_kind="site_pages",
            )
        )

    def close(self) -> None:
        self.store.close()


# 1. Shape of both files, built from a real manifest -------------------------------------------


def test_manifest_and_surface_shapes(tmp_path: Path):
    fx = Fixture(tmp_path)
    generated_at = 1_700_000_500.0
    try:
        out = tmp_path / "out"
        report = build_static_index(
            store=fx.store, root=fx.root, out=out, generated_at=generated_at
        )
        expected_counts = fx.store.manifest_counts().as_dict()
        expected_kinds = {
            kc.source_kind: kc.counts.as_dict() for kc in fx.store.manifest_kind_counts()
        }
        expected_db_sha256 = hashlib.sha256(fx.db_path.read_bytes()).hexdigest()
    finally:
        fx.close()

    assert isinstance(report, PublishReport)
    assert report.documents == 4
    assert report.counts.as_dict() == expected_counts == {
        "indexed": 2, "failed": 1, "superseded": 1, "refused": 0,
    }

    manifest_path = Path(report.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "openagentsearch.static-index/1"
    assert manifest["counts"] == expected_counts
    assert manifest["kinds"] == expected_kinds
    assert manifest["db_sha256"] == report.db_sha256 == expected_db_sha256
    parsed_ts = datetime.fromisoformat(manifest["generated_at"].replace("Z", "+00:00"))
    assert parsed_ts.tzinfo is not None and abs(parsed_ts.timestamp() - generated_at) < 0.001

    documents = manifest["documents"]
    assert len(documents) == report.documents == 4
    assert documents == sorted(documents, key=lambda d: (d["source_url"], d["doc_sha256"]))
    statuses = sorted(d["status"] for d in documents)
    assert statuses == ["failed", "indexed", "indexed", "superseded"]
    for doc in documents:
        assert set(doc.keys()) == {
            "doc_sha256", "source_url", "status", "reason", "indexed_at", "chunk_count",
            "extracted_sha256", "source_kind",
        }

    surface_path = Path(report.surface_path)
    lines = [json.loads(line) for line in surface_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == report.surface_lines == 2
    assert [line["kind"] for line in lines] == sorted(line["kind"] for line in lines)

    by_sha = {line["doc_sha256"]: line for line in lines}
    assert set(by_sha) == {fx.sha_b, fx.sha_c}

    plain = by_sha[fx.sha_b]
    assert set(plain.keys()) == {
        "doc_sha256", "url", "kind", "title", "section", "abstract", "chunk_count", "indexed_at",
    }
    assert plain["url"] == fx.url_a
    assert plain["kind"] == "site_pages"
    assert plain["title"] == "Guide v2"
    assert plain["section"] is None
    assert plain["abstract"] == "Guide version two body text for the abstract to chew on."
    assert plain["chunk_count"] == 4

    sectioned = by_sha[fx.sha_c]
    assert sectioned["url"] == fx.url_c
    assert sectioned["kind"] == "github_docs"
    assert sectioned["title"] == "Getting Started"
    assert sectioned["section"] == "getting-started"
    collapsed = " ".join(fx.long_text.split())
    abstract = sectioned["abstract"]
    assert len(abstract) <= 300
    assert abstract == collapsed[: len(abstract)]
    # cut at a word boundary: the next character in the collapsed text is a space
    assert len(abstract) < len(collapsed) and collapsed[len(abstract)] == " "

    assert report.missing_extracted == 0
    readme = (manifest_path.parent / "README.txt").read_text(encoding="utf-8")
    assert len(readme.splitlines()) <= 20
    assert "manifest.json" in readme and "flop-surface.jsonl" in readme
    assert "superseded" in readme and "ranking" in readme


# 2. Missing extracted record never fails the export ---------------------------------------------


def test_missing_extracted_record_is_reported_not_fatal(tmp_path: Path):
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

    assert report.missing_extracted == 1
    surface_text = Path(report.surface_path).read_text(encoding="utf-8")
    lines = [json.loads(line) for line in surface_text.splitlines()]
    assert len(lines) == 1
    assert lines[0]["title"] == ""
    assert lines[0]["abstract"] == ""


def test_undecodable_extracted_record_is_reported_not_fatal(tmp_path: Path):
    """An `extracted/<sha>.json` file that exists but is not valid UTF-8 must degrade the same
    way a missing file does -- `""`/`""` and one `missing_extracted` count -- never crash the
    whole export with an uncaught `UnicodeDecodeError`."""
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    try:
        doc_sha256 = _sha("bad-utf8")
        store.record_manifest(
            ManifestEntry(
                doc_sha256=doc_sha256, source_url="https://docs.example.test/badutf8",
                status="indexed", reason="", indexed_at=1_700_000_000.0, chunk_count=1,
                extracted_sha256=_sha("bad-utf8-text"), source_kind="site_pages",
            )
        )
        extracted_dir = root / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        (extracted_dir / f"{doc_sha256}.json").write_bytes(
            b'{"title": "x", "text": "hello \xff\xfe world"}'
        )
        report = build_static_index(store=store, root=root, out=tmp_path / "out")
    finally:
        store.close()

    assert report.missing_extracted == 1
    surface_text = Path(report.surface_path).read_text(encoding="utf-8")
    lines = [json.loads(line) for line in surface_text.splitlines()]
    assert len(lines) == 1
    assert lines[0]["title"] == ""
    assert lines[0]["abstract"] == ""


# 2b. flop-surface.jsonl sort order: same source_kind, tie broken by source_url -----------------


def test_surface_sort_order_ties_broken_by_source_url(tmp_path: Path):
    """Two indexed documents share one `source_kind` and are inserted in the opposite of
    URL-sorted order, so this only passes if `flop-surface.jsonl` is actually sorted by the full
    `(source_kind, source_url, doc_sha256)` key -- not just `source_kind`, which a single document
    per kind (as in `Fixture`) can never distinguish from a bug that dropped the secondary/
    tertiary keys."""
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "v.sqlite3", dimension=2)
    raw = RawStore(root)
    extracted = ExtractStore(root)
    try:
        url_later = "https://docs.example.test/z-page"
        sha_later = raw.put(url_later, b"<html>z page</html>", 200, True, 1_700_001_000.0)
        store.record_manifest(
            ManifestEntry(
                doc_sha256=sha_later, source_url=url_later, status="indexed", reason="",
                indexed_at=1_700_001_000.0, chunk_count=1, extracted_sha256=_sha("z-text"),
                source_kind="site_pages",
            )
        )
        extracted.put(
            sha_later, url_later, {"text": "z page body", "title": "Z", "lang": "en"},
            1_700_001_001.0,
        )

        url_earlier = "https://docs.example.test/a-page"
        sha_earlier = raw.put(url_earlier, b"<html>a page</html>", 200, True, 1_700_001_100.0)
        store.record_manifest(
            ManifestEntry(
                doc_sha256=sha_earlier, source_url=url_earlier, status="indexed", reason="",
                indexed_at=1_700_001_100.0, chunk_count=1, extracted_sha256=_sha("a-text"),
                source_kind="site_pages",
            )
        )
        extracted.put(
            sha_earlier, url_earlier, {"text": "a page body", "title": "A", "lang": "en"},
            1_700_001_101.0,
        )

        report = build_static_index(store=store, root=root, out=tmp_path / "out")
    finally:
        store.close()

    lines = [
        json.loads(line)
        for line in Path(report.surface_path).read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 2
    assert lines == sorted(lines, key=lambda line: (line["kind"], line["url"], line["doc_sha256"]))
    assert [line["url"] for line in lines] == [url_earlier, url_later]


# 3. Determinism: same generated_at -> byte-identical files --------------------------------------


def test_same_generated_at_is_byte_identical_across_runs(tmp_path: Path):
    fx = Fixture(tmp_path)
    try:
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        r1 = build_static_index(
            store=fx.store, root=fx.root, out=out1, generated_at=1_700_000_999.0
        )
        r2 = build_static_index(
            store=fx.store, root=fx.root, out=out2, generated_at=1_700_000_999.0
        )
    finally:
        fx.close()

    assert Path(r1.manifest_path).read_bytes() == Path(r2.manifest_path).read_bytes()
    assert Path(r1.surface_path).read_bytes() == Path(r2.surface_path).read_bytes()


# 4. Atomicity: a corrupt store leaves a pre-existing manifest.json untouched --------------------


def test_corrupt_manifest_leaves_existing_output_untouched(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    db_path = root / "v.sqlite3"
    store = VectorStore(db_path, dimension=2)
    out = tmp_path / "out"
    index_dir = out / "index"
    index_dir.mkdir(parents=True)
    manifest_path = index_dir / "manifest.json"
    original_bytes = b'{"schema": "pre-existing", "documents": []}'
    manifest_path.write_bytes(original_bytes)

    try:
        side_conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            with side_conn:
                side_conn.execute(
                    "INSERT INTO manifest (doc_sha256, source_url, status, reason, indexed_at, "
                    "chunk_count, extracted_sha256, source_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_sha("bogus"), "https://a.test/x", "bogus-status", "", 1.0, 0, "", "html"),
                )
        finally:
            side_conn.close()

        with pytest.raises(ManifestCorruptionError):
            build_static_index(store=store, root=root, out=out)
    finally:
        store.close()

    assert manifest_path.read_bytes() == original_bytes
    leftover = sorted(p.name for p in index_dir.iterdir())
    assert leftover == ["manifest.json"], leftover


# 5. CLI: subprocess success and a missing --db ---------------------------------------------------


def test_cli_prints_one_json_line_and_missing_db_exits_2(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.close()  # close before handing the same file to a fresh subprocess-owned connection

    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.pipeline.publish",
            "--db", str(fx.db_path), "--root", str(fx.root), "--out", str(out_dir),
            "--abstract-chars", "120",
            "--base-url", "https://djd39448.github.io/openagentsearch",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 1, stdout_lines
    payload = json.loads(stdout_lines[0])
    assert payload["documents"] == 4
    assert payload["surface_lines"] == 2
    assert (out_dir / "index" / "manifest.json").is_file()
    assert (out_dir / "index" / "flop-surface.jsonl").is_file()
    assert (out_dir / "index" / "README.txt").is_file()

    missing_db = tmp_path / "does-not-exist.sqlite3"
    proc2 = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.pipeline.publish",
            "--db", str(missing_db), "--root", str(fx.root), "--out", str(tmp_path / "out2"),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc2.returncode == 2, (proc2.stdout, proc2.stderr)
    assert proc2.stdout == ""
    stderr_lines = [line for line in proc2.stderr.splitlines() if line.strip()]
    assert len(stderr_lines) == 1, stderr_lines
    error_payload = json.loads(stderr_lines[0])
    assert "error" in error_payload
