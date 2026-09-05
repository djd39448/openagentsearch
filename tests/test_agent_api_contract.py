"""Tests for the documented agent API contract: the constant, and the doc that must match it."""

import json
import pathlib

from openagentsearch.api.contract import AGENT_API_CONTRACT

DOC_PATH = pathlib.Path(__file__).resolve().parents[1] / "docs" / "agent-api.md"
SEARCH = "GET /search"
DOC = "GET /doc/{doc_sha256}"


def test_contract_has_exactly_two_endpoints_and_is_json_compatible():
    assert set(AGENT_API_CONTRACT) == {SEARCH, DOC}
    # JSON-compatible data only: a dumps/loads round trip must reproduce the constant exactly.
    assert json.loads(json.dumps(AGENT_API_CONTRACT)) == AGENT_API_CONTRACT


def test_search_contract_captures_validation_fields_and_errors():
    c = AGENT_API_CONTRACT[SEARCH]
    q = c["params"]["q"]
    assert (q["in"], q["type"], q["required"]) == ("query", "string", True)
    assert q["constraints"]["max_length"] == 512
    assert q["constraints"]["non_empty_after_strip"] is True
    assert "verbatim" in q["notes"]
    k = c["params"]["k"]
    assert (k["in"], k["type"], k["required"], k["default"]) == ("query", "integer", False, 10)
    assert (k["constraints"]["min"], k["constraints"]["max"], k["constraints"]["syntax"]) == (1, 50, "ascii_digits")

    assert c["success"]["status"] == 200
    assert c["success"]["keys"] == ["query", "k", "results"]
    assert c["success"]["nested"]["results"]["keys"] == ["chunk_id", "doc_sha256", "doc_url", "score", "snippet"]
    assert c["success"]["nested"]["results"]["nullable"] == ["doc_url"]

    assert [(e["status"], e["error"]) for e in c["errors"]] == [
        (400, "missing_query"),
        (400, "query_too_long"),
        (400, "invalid_k"),
    ]


def test_doc_contract_captures_hash_format_fields_and_errors():
    c = AGENT_API_CONTRACT[DOC]
    p = c["params"]["doc_sha256"]
    assert (p["in"], p["type"], p["required"]) == ("path", "string", True)
    assert p["constraints"]["pattern"] == "[0-9a-f]{64}"
    assert p["constraints"]["case"] == "lower"

    assert c["success"]["status"] == 200
    assert c["success"]["keys"] == ["doc_sha256", "url", "title", "lang", "text", "extracted_at", "provenance"]
    assert c["success"]["nested"]["provenance"]["keys"] == ["url", "fetched_at", "status", "sha256", "robots_allowed"]
    assert c["success"]["nested"]["provenance"]["nullable"] == ["provenance"]

    assert [(e["status"], e["error"]) for e in c["errors"]] == [
        (400, "invalid_sha256"),
        (404, "not_found"),
    ]


def _contract_json_blocks(markdown: str) -> dict[str, dict]:
    """Map each '## <endpoint>' section to the JSON object under its '### Contract JSON' heading."""
    blocks: dict[str, dict] = {}
    endpoint = None
    in_contract = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            endpoint = line[3:].strip()
            in_contract = False
        elif line.strip() == "### Contract JSON":
            in_contract = True
        elif in_contract and line.startswith("{"):
            assert endpoint is not None
            assert endpoint not in blocks, f"two Contract JSON blocks for {endpoint}"
            blocks[endpoint] = json.loads(line)
            in_contract = False
    return blocks


def test_doc_contract_json_matches_the_constant_exactly():
    markdown = DOC_PATH.read_text(encoding="utf-8")
    blocks = _contract_json_blocks(markdown)
    assert set(blocks) == {SEARCH, DOC}
    for endpoint in (SEARCH, DOC):
        assert blocks[endpoint] == AGENT_API_CONTRACT[endpoint], f"docs/agent-api.md drifted for {endpoint}"
        for heading in ("### Parameters", "### Success fields", "### Nested fields", "### Errors"):
            section_start = markdown.index(f"## {endpoint}")
            assert heading in markdown[section_start:], f"{endpoint} lacks {heading}"
