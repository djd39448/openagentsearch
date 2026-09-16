"""Package C2a, spec Tests item 5: `python -m openagentsearch.pipeline.lexical` as a subprocess.

Everything here is offline; every subprocess call carries a timeout so a bug here can never hang
the suite, and no path is machine-specific (all come from pytest's `tmp_path`).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from openagentsearch.extract.store import ExtractStore
from openagentsearch.fetch.rawstore import RawStore
from openagentsearch.index.manifest import ManifestEntry
from openagentsearch.vector.store import VectorStore

REPO = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT = 60


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return env


def _make_fixture_db(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal on-disk store: one indexed document with real raw + extracted records, closed
    before returning (so a fresh subprocess-owned connection can open the same file)."""
    root = tmp_path / "root"
    root.mkdir()
    store = VectorStore(root / "vectors.sqlite3", dimension=2)
    raw = RawStore(root)
    extracted = ExtractStore(root)
    url = "https://docs.example.test/cli-guide"
    text = "CLI guide covering the pipeline_lexical command and its report line."
    sha256 = raw.put(url, text.encode("utf-8"), 200, True, 1_700_000_000.0)
    store.record_manifest(
        ManifestEntry(
            doc_sha256=sha256, source_url=url, status="indexed", reason="",
            indexed_at=1_700_000_000.0, chunk_count=1, extracted_sha256=sha256,
            source_kind="site_pages",
        )
    )
    extracted.put(sha256, url, {"text": text, "title": "CLI Guide", "lang": "en"}, 1_700_000_001.0)
    store.close()
    return root / "vectors.sqlite3", root


def test_cli_writes_the_file_and_prints_a_report_line(tmp_path: Path):
    db_path, root = _make_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.pipeline.lexical",
            "--db", str(db_path), "--root", str(root), "--out", str(out_dir),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 1, stdout_lines
    payload = json.loads(stdout_lines[0])
    assert set(payload.keys()) == {
        "path", "bytes", "docs", "terms", "postings", "dropped_terms", "missing_extracted",
        "seconds",
    }
    assert payload["docs"] == 1
    assert payload["missing_extracted"] == 0
    assert payload["bytes"] > 0

    written_path = out_dir / "index" / "lexical-v1.json"
    assert written_path.is_file()
    assert written_path.stat().st_size == payload["bytes"]
    assert payload["path"] == str(written_path)


def test_cli_max_bytes_refusal_exits_1_and_writes_nothing(tmp_path: Path):
    db_path, root = _make_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.pipeline.lexical",
            "--db", str(db_path), "--root", str(root), "--out", str(out_dir),
            "--max-bytes", "10",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""

    stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(stderr_lines) == 1, stderr_lines
    error_payload = json.loads(stderr_lines[0])
    assert "error" in error_payload
    assert "LexicalSizeError" in error_payload["error"]

    assert not (out_dir / "index").exists()


def test_cli_missing_db_exits_1_with_json_error(tmp_path: Path):
    _db_path, root = _make_fixture_db(tmp_path)
    missing_db = tmp_path / "does-not-exist.sqlite3"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.pipeline.lexical",
            "--db", str(missing_db), "--root", str(root), "--out", str(tmp_path / "out"),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    error_payload = json.loads(proc.stderr.strip())
    assert "error" in error_payload


def test_cli_missing_required_flag_exits_2(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "openagentsearch.pipeline.lexical", "--root", str(tmp_path)],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
