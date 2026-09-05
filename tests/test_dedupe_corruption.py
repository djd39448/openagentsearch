"""Post-roadmap #4: the dedupe scan fails loudly on a damaged corpus and really dedupes across distinct raw documents."""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from openagentsearch.extract.dedupe import DedupingExtractStore


def _seed_raw(root: Path, raw: bytes, url: str) -> str:
    """Write raw/<sha>.html and a matching provenance line; return the sha."""
    sha = hashlib.sha256(raw).hexdigest()
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw" / f"{sha}.html").write_bytes(raw)
    with (root / "raw" / "provenance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"url": url, "fetched_at": 1700000000.0, "status": 200, "sha256": sha, "robots_allowed": True}) + "\n")
    return sha


def _extracted(text: str) -> dict:
    return {"text": text, "title": "T", "lang": "en"}


def test_dedupe_rejects_malformed_existing_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        sha = _seed_raw(root, b"<p>candidate</p>", "https://x.test/c")
        (root / "extracted").mkdir()
        (root / "extracted" / "broken.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError) as info:
            DedupingExtractStore(root).put(sha, "https://x.test/c", _extracted("candidate"), 1700000001.0)
        assert "broken.json" in str(info.value)
        assert isinstance(info.value.__cause__, json.JSONDecodeError)
        assert not (root / "extracted" / f"{sha}.json").exists()


def test_dedupe_rejects_invalid_existing_record_shape():
    shapes = {
        "list.json": json.dumps(["not", "an", "object"]),
        "missing_text.json": json.dumps({"url": "u", "title": "T", "lang": "en"}),
        "text_not_string.json": json.dumps({"url": "u", "title": "T", "lang": "en", "text": 42}),
    }
    for name, body in shapes.items():
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sha = _seed_raw(root, b"<p>candidate</p>", "https://x.test/c")
            (root / "extracted").mkdir()
            (root / "extracted" / name).write_text(body, encoding="utf-8")
            with pytest.raises(ValueError) as info:
                DedupingExtractStore(root).put(sha, "https://x.test/c", _extracted("candidate"), 1700000001.0)
            assert name in str(info.value)
            if name == "missing_text.json":
                assert isinstance(info.value.__cause__, KeyError)
            assert not (root / "extracted" / f"{sha}.json").exists()


def test_dedupe_distinct_raw_hashes_same_extracted_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        first_raw = b"<html><body><p>Same visible text.</p></body></html>"
        second_raw = b"<html><body><!-- different bytes --><p>Same visible text.</p></body></html>"
        assert first_raw != second_raw
        first_sha = _seed_raw(root, first_raw, "https://x.test/1")
        second_sha = _seed_raw(root, second_raw, "https://x.test/2")
        assert first_sha != second_sha
        store = DedupingExtractStore(root)
        first_path = store.put(first_sha, "https://x.test/1", _extracted("Same visible text."), 1700000001.0)
        assert first_path is not None and first_path.exists()
        assert store.put(second_sha, "https://x.test/2", _extracted("Same visible text."), 1700000002.0) is None
        records = list((root / "extracted").glob("*.json"))
        assert records == [root / "extracted" / f"{first_sha}.json"]
        assert not (root / "extracted" / f"{second_sha}.json").exists()
