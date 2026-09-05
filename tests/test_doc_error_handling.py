"""P6 hardening: provenance/extracted-document error handling of the /doc route."""

import builtins
import json
import tempfile
import threading
import urllib.request
from pathlib import Path

from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.server import create_server

SHA = "a1" * 32  # exactly 64 lowercase hex characters
URL = "https://example.test/page"
PROVENANCE_OK = {"url": URL, "fetched_at": 1234567800.0, "status": 200, "sha256": SHA, "robots_allowed": True}


def _write_extracted(root: Path, text: str = "extracted text") -> None:
    (root / "extracted").mkdir(parents=True, exist_ok=True)
    doc = {"url": URL, "title": "Title", "lang": "en", "text": text, "extracted_at": 1234567890.0}
    (root / "extracted" / f"{SHA}.json").write_text(json.dumps(doc), encoding="utf-8")


def _write_provenance(root: Path, lines: list[str]) -> None:
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "provenance.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_malformed_unrelated_line_is_skipped_and_valid_match_returned_over_http():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_extracted(root)
        _write_provenance(root, ["this is not json {{{", json.dumps(PROVENANCE_OK)])
        server = create_server("127.0.0.1", 0, prefix_routes={"/doc/": make_doc_route(root)})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/doc/{SHA}"
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.getcode() == 200
                body = json.loads(response.read().decode("utf-8"))
            assert body["doc_sha256"] == SHA
            assert body["provenance"] == PROVENANCE_OK
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def test_matching_provenance_missing_a_field_raises_value_error_with_key_error_cause():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_extracted(root)
        incomplete = {k: v for k, v in PROVENANCE_OK.items() if k != "status"}
        _write_provenance(root, [json.dumps(incomplete)])
        route = make_doc_route(root)
        try:
            route(SHA, {})
            assert False, "a matching entry missing a required field must raise, not be skipped"
        except ValueError as exc:
            assert "Malformed matching provenance entry" in str(exc)
            assert isinstance(exc.__cause__, KeyError)


def _open_raising_for(target: Path, real_open):
    """A builtins.open replacement that raises OSError only for `target`; everything else is delegated."""

    def fake_open(file, *args, **kwargs):
        if Path(str(file)) == target:
            raise OSError("simulated read failure")
        return real_open(file, *args, **kwargs)

    return fake_open


def test_provenance_read_failure_raises_value_error_with_os_error_cause():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_extracted(root)
        _write_provenance(root, [json.dumps(PROVENANCE_OK)])
        route = make_doc_route(root)
        real_open = builtins.open
        builtins.open = _open_raising_for(root / "raw" / "provenance.jsonl", real_open)
        try:
            try:
                route(SHA, {})
                assert False, "a provenance read failure must not become provenance: null"
            except ValueError as exc:
                assert "Error reading provenance" in str(exc)
                assert isinstance(exc.__cause__, OSError)
        finally:
            builtins.open = real_open
        # With the real open restored the same request succeeds again.
        status, body = route(SHA, {})
        assert status == 200 and body["provenance"] == PROVENANCE_OK


def test_extracted_document_read_failure_raises_value_error_with_os_error_cause():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_extracted(root)
        route = make_doc_route(root)
        real_open = builtins.open
        builtins.open = _open_raising_for(root / "extracted" / f"{SHA}.json", real_open)
        try:
            try:
                route(SHA, {})
                assert False, "an extracted-document read failure must raise, not 404"
            except ValueError as exc:
                assert "Error reading extracted document" in str(exc)
                assert isinstance(exc.__cause__, OSError)
        finally:
            builtins.open = real_open
        status, _ = route(SHA, {})
        assert status == 200
