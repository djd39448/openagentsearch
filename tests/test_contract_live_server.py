"""P5.1b: the machine-readable contract must match what the live server actually emits."""

import json
import tempfile
import threading
import urllib.request
from pathlib import Path

from openagentsearch.api.contract import AGENT_API_CONTRACT
from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.search import make_search_route
from openagentsearch.api.server import create_server
from openagentsearch.vector.store import VectorStore

SEARCH = AGENT_API_CONTRACT["GET /search"]["success"]
DOC = AGENT_API_CONTRACT["GET /doc/{doc_sha256}"]["success"]
WITH_PROVENANCE = "a" * 64
WITHOUT_PROVENANCE = "b" * 64
URL = "https://example.test/page"


class StubEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


def _write_doc(root: Path, sha: str, url: str) -> None:
    (root / "extracted").mkdir(parents=True, exist_ok=True)
    doc = {"url": url, "title": "Title", "lang": "en", "text": "extracted text", "extracted_at": 1234567890.0}
    (root / "extracted" / f"{sha}.json").write_text(json.dumps(doc), encoding="utf-8")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.getcode() == 200
        return json.loads(response.read().decode("utf-8"))  # dicts preserve emitted key order


def test_contract_matches_live_endpoint_behaviour():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = VectorStore(root / "vectors.sqlite3", 2)
        store.add("chunk-1", WITH_PROVENANCE, [1.0, 0.0], "the only chunk")
        _write_doc(root, WITH_PROVENANCE, URL)
        _write_doc(root, WITHOUT_PROVENANCE, "https://example.test/other")
        (root / "raw").mkdir(exist_ok=True)
        provenance = {"url": URL, "fetched_at": 1234567800.0, "status": 200, "sha256": WITH_PROVENANCE, "robots_allowed": True}
        (root / "raw" / "provenance.jsonl").write_text(json.dumps(provenance) + "\n", encoding="utf-8")

        server = create_server(
            "127.0.0.1",
            0,
            routes={"/search": make_search_route(store, StubEmbedder(), lambda sha: None)},
            prefix_routes={"/doc/": make_doc_route(root)},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # /search: top-level keys in emitted order, result keys in emitted order, null doc_url live.
            body = _get_json(base + "/search?q=alpha&k=5")
            assert list(body.keys()) == SEARCH["keys"]
            assert len(body["results"]) == 1
            for result in body["results"]:
                assert list(result.keys()) == SEARCH["nested"]["results"]["keys"]
                assert result["doc_url"] is None
            assert SEARCH["nullable"] == ["results[].doc_url"]

            # /doc: top-level keys in emitted order; non-null provenance keys in emitted order.
            body = _get_json(base + f"/doc/{WITH_PROVENANCE}")
            assert list(body.keys()) == DOC["keys"]
            assert body["provenance"] is not None
            assert list(body["provenance"].keys()) == DOC["nested"]["provenance"]["keys"]

            # /doc for a known document with no matching provenance: the declared nullable field is null.
            body = _get_json(base + f"/doc/{WITHOUT_PROVENANCE}")
            assert list(body.keys()) == DOC["keys"]
            assert body["provenance"] is None
            assert DOC["nullable"] == ["provenance"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            store.close()
