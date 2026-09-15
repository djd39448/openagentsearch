"""Tests for `python -m openagentsearch.api.server` (openagentsearch.api.cli), driven as a real
subprocess for the startup/HTTP/shutdown lifecycle, and in-process for argument validation.

Every socket call has a timeout, every subprocess read goes through a reader-thread queue with a
timeout, and every subprocess is killed in a `finally` block so a bug here can never hang the
test suite."""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from openagentsearch.api.cli import _LOOPBACK_HOSTS
from openagentsearch.api.cli import main as cli_main
from openagentsearch.embed.keyword import KeywordEmbedder
from openagentsearch.extract.html import extract
from openagentsearch.extract.store import ExtractStore
from openagentsearch.pipeline.index import index_document
from openagentsearch.vector.store import VectorStore

REPO = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 20.0
STOP_TIMEOUT = 15.0
HTTP_TIMEOUT = 5.0
ZERO_SHA = "0" * 64

SOURCE_URL = "https://docs.example.test/gizmo-guide"
TARGET_WORD = "gizmo"
RAW_HTML = (
    '<html lang="en"><head><title>Gizmo Guide</title></head><body>'
    "<p>This page explains the gizmo in careful detail for agents.</p>"
    "</body></html>"
)


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return env


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
            return response.getcode(), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class ServerProcess:
    """Launches `python -m openagentsearch.api.server` as a real subprocess. Every stdout read
    goes through a background reader thread and a bounded `queue.Queue.get()`, so a stuck server
    fails the test with a timeout instead of hanging it."""

    def __init__(self, args: list[str]) -> None:
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(REPO),
            env=_subprocess_env(),
        )
        if sys.platform == "win32":
            # So CTRL_BREAK_EVENT (sent in send_stop_signal) reaches only this child, never the
            # test process itself.
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openagentsearch.api.server", *args], **popen_kwargs
        )
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def read_line(self, timeout: float = STARTUP_TIMEOUT) -> str:
        return self._lines.get(timeout=timeout).rstrip("\n")

    def send_stop_signal(self) -> None:
        if sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.terminate()

    def wait(self, timeout: float = STOP_TIMEOUT) -> int:
        return self.proc.wait(timeout=timeout)

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass


def _seed_root(root: Path) -> tuple[str, Path]:
    """Index one document with the real pipeline + KeywordEmbedder(256) into root/v.sqlite, then
    populate root/raw/, root/raw/provenance.jsonl and root/extracted/ the same way
    tests/test_offline_api_integration.py does. Returns (doc_sha256, db_path)."""
    db_path = root / "v.sqlite"
    store = VectorStore(db_path, 256)
    embedder = KeywordEmbedder(256)
    try:
        report = index_document(
            RAW_HTML, SOURCE_URL, store=store, embedder=embedder, chunk_size=64, overlap=0
        )
    finally:
        store.close()
    extracted = extract(RAW_HTML)
    raw_bytes = RAW_HTML.encode("utf-8")
    (root / "raw").mkdir()
    (root / "raw" / f"{report.doc_sha256}.html").write_bytes(raw_bytes)
    provenance = {
        "url": SOURCE_URL,
        "fetched_at": 1700000000.0,
        "status": 200,
        "sha256": report.doc_sha256,
        "robots_allowed": True,
    }
    (root / "raw" / "provenance.jsonl").write_text(
        json.dumps(provenance, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    ExtractStore(root).put(report.doc_sha256, SOURCE_URL, extracted, 1700000001.0)
    return report.doc_sha256, db_path


# ---------------------------------------------------------------------------------------------
# 1. Done-when: start, healthz/search/doc over real HTTP, stop cleanly.
# ---------------------------------------------------------------------------------------------


def test_done_when_start_serve_and_stop_over_real_subprocess():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "v.sqlite"
        server = ServerProcess(
            ["--db", str(db), "--embedder", "keyword", "--host", "127.0.0.1", "--port", "0"]
        )
        try:
            listening = json.loads(server.read_line())
            assert set(listening) == {"listening", "db", "embedder", "doc_route"}
            assert listening["embedder"] == "keyword"
            assert listening["doc_route"] is False
            assert listening["db"] == str(db)
            base = listening["listening"]
            assert base.startswith("http://127.0.0.1:")

            status, body = _get(base + "/healthz")
            assert status == 200
            assert body == {
                "status": "ok",
                "index": {"indexed": 0, "failed": 0, "superseded": 0, "refused": 0},
                "kinds": {},  # per-source-kind counts (A3); empty for an empty store
            }

            status, body = _get(base + "/search?q=hello")
            assert status == 200
            assert body["results"] == []

            status, body = _get(base + f"/doc/{ZERO_SHA}")
            assert status == 404  # /doc/ is never mounted without --root
            assert body == {"error": "not_found"}

            server.send_stop_signal()
            stopped_line = server.read_line(timeout=STOP_TIMEOUT)
            assert stopped_line == '{"stopped":true}'
            exit_code = server.wait(timeout=STOP_TIMEOUT)
            assert exit_code == 0
        finally:
            server.kill()


# ---------------------------------------------------------------------------------------------
# 2. Pre-populated DB + --root: search and doc endpoints agree with the manifest, healthz counts.
# ---------------------------------------------------------------------------------------------


def test_prepopulated_db_and_root_are_searchable_and_fetchable():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        doc_sha256, db_path = _seed_root(root)
        server = ServerProcess(
            ["--db", str(db_path), "--embedder", "keyword", "--port", "0", "--root", str(root)]
        )
        try:
            listening = json.loads(server.read_line())
            assert listening["doc_route"] is True
            base = listening["listening"]

            status, body = _get(base + f"/search?q={TARGET_WORD}")
            assert status == 200
            assert len(body["results"]) >= 1
            first = body["results"][0]
            assert first["doc_sha256"] == doc_sha256
            assert first["doc_url"] == SOURCE_URL  # resolved through the index manifest

            status, doc = _get(base + f"/doc/{doc_sha256}")
            assert status == 200
            assert doc["url"] == SOURCE_URL
            assert doc["doc_sha256"] == doc_sha256

            status, health = _get(base + "/healthz")
            assert status == 200
            assert health["index"]["indexed"] == 1

            server.send_stop_signal()
            stopped_line = server.read_line(timeout=STOP_TIMEOUT)
            assert stopped_line == '{"stopped":true}'
            exit_code = server.wait(timeout=STOP_TIMEOUT)
            assert exit_code == 0
        finally:
            server.kill()


# ---------------------------------------------------------------------------------------------
# 3. Argument validation, in-process (no subprocess needed for these).
# ---------------------------------------------------------------------------------------------


def test_bad_embedder_choice_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "bogus"])
    assert exc_info.value.code == 2


def test_port_out_of_range_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "keyword", "--port", "70000"])
    assert exc_info.value.code == 2


def test_negative_port_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "keyword", "--port", "-1"])
    assert exc_info.value.code == 2


def test_db_in_missing_directory_exits_1_with_json_error(tmp_path, capsys):
    missing = tmp_path / "does" / "not" / "exist" / "v.sqlite"
    exit_code = cli_main(["--db", str(missing), "--embedder", "keyword", "--port", "0"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout: the server never started listening
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}
    assert isinstance(error["error"], str) and error["error"]


def test_root_pointing_at_a_file_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    not_a_dir = tmp_path / "plainfile.txt"
    not_a_dir.write_text("just a file, not a directory", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "keyword", "--root", str(not_a_dir)])
    assert exc_info.value.code == 2


def test_root_pointing_at_a_missing_path_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    missing = tmp_path / "does-not-exist-at-all"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "keyword", "--root", str(missing)])
    assert exc_info.value.code == 2


def test_missing_required_arguments_exit_2(tmp_path):
    db = tmp_path / "v.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--embedder", "keyword"])  # no --db
    assert exc_info.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db)])  # no --embedder
    assert exc_info.value.code == 2


def test_dimension_zero_or_negative_exits_2(tmp_path):
    db = tmp_path / "v.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--db", str(db), "--embedder", "keyword", "--dimension", "0"])
    assert exc_info.value.code == 2


def test_build_server_closes_store_on_later_construction_failure(tmp_path, capsys):
    # --dimension 4 passes argparse's own `> 0` check but is rejected inside KeywordEmbedder's
    # own `>= 8` floor - by the time that raises, build_server() has already opened the
    # VectorStore on --db. If build_server() failed to close that store before re-raising, its
    # sqlite file handle would stay open and this file could not be deleted on Windows.
    db = tmp_path / "v.sqlite"
    exit_code = cli_main(
        ["--db", str(db), "--embedder", "keyword", "--dimension", "4", "--port", "0"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    error = json.loads(stderr_lines[0])
    assert "dimension" in error["error"]
    db.unlink()  # raises PermissionError on Windows if the store's file handle leaked


def test_ipv6_loopback_literal_is_not_treated_as_loopback_and_fails_to_bind(tmp_path, capsys):
    # "::1" is deliberately excluded from _LOOPBACK_HOSTS: create_server() builds a stdlib
    # http.server.ThreadingHTTPServer, whose address_family is hardcoded to socket.AF_INET, so
    # binding "::1" always fails (gaierror) rather than serving over IPv6. Because that failure
    # happens inside build_server(), it is reported through the same {"error": "..."} / exit-1
    # contract as any other pre-listening failure, and the non-loopback warning is never reached.
    assert "::1" not in _LOOPBACK_HOSTS

    db = tmp_path / "v.sqlite"
    exit_code = cli_main(["--db", str(db), "--embedder", "keyword", "--host", "::1", "--port", "0"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # never reached listening
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1  # the bind failure only, no separate warning line
    error = json.loads(stderr_lines[0])
    assert set(error) == {"error"}


# ---------------------------------------------------------------------------------------------
# 4. The startup line is machine-readable: --port 0 reports the real, non-zero bound port.
# ---------------------------------------------------------------------------------------------


def test_ephemeral_port_reports_real_port_in_listening_line():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "v.sqlite"
        server = ServerProcess(["--db", str(db), "--embedder", "keyword", "--port", "0"])
        try:
            listening = json.loads(server.read_line())
            assert set(listening) == {"listening", "db", "embedder", "doc_route"}
            host_port = listening["listening"]
            assert host_port.startswith("http://")
            port = int(host_port.rsplit(":", 1)[1])
            assert 1 <= port <= 65535
            assert port != 0

            server.send_stop_signal()
            server.read_line(timeout=STOP_TIMEOUT)
            assert server.wait(timeout=STOP_TIMEOUT) == 0
        finally:
            server.kill()
