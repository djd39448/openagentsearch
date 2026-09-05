"""Tests for the documented agent API contract."""

import json
import pathlib

from openagentsearch.api.contract import AGENT_API_CONTRACT


def test_contract_has_correct_endpoints():
    """Test that the contract has the correct two endpoints."""
    assert "GET /search" in AGENT_API_CONTRACT
    assert "GET /doc/{doc_sha256}" in AGENT_API_CONTRACT


def test_search_endpoint_structure():
    """Test that the /search endpoint captures all required validation and structure."""
    search_contract = AGENT_API_CONTRACT["GET /search"]
    
    # Check params
    assert "q" in search_contract["params"]
    assert search_contract["params"]["q"]["in"] == "query"
    assert search_contract["params"]["q"]["type"] == "string"
    assert search_contract["params"]["q"]["required"] is True
    
    # Check k param
    assert "k" in search_contract["params"]
    assert search_contract["params"]["k"]["in"] == "query"
    assert search_contract["params"]["k"]["type"] == "integer"
    assert search_contract["params"]["k"]["required"] is False
    assert search_contract["params"]["k"]["default"] == 10
    
    # Check success keys
    assert search_contract["success"]["status"] == 200
    assert search_contract["success"]["keys"] == ["query", "k", "results"]
    
    # Check result object structure
    result_keys = search_contract["success"]["nested"]["results"]["keys"]
    expected_result_keys = ["chunk_id", "doc_sha256", "doc_url", "score", "snippet"]
    assert result_keys == expected_result_keys
    
    # Check errors
    error_codes = [err["error"] for err in search_contract["errors"]]
    assert "missing_query" in error_codes
    assert "query_too_long" in error_codes
    assert "invalid_k" in error_codes


def test_doc_endpoint_structure():
    """Test that the /doc/{doc_sha256} endpoint captures all required formats and structure."""
    doc_contract = AGENT_API_CONTRACT["GET /doc/{doc_sha256}"]
    
    # Check param format
    assert "doc_sha256" in doc_contract["params"]
    assert doc_contract["params"]["doc_sha256"]["in"] == "path"
    assert doc_contract["params"]["doc_sha256"]["type"] == "string"
    assert doc_contract["params"]["doc_sha256"]["required"] is True
    assert doc_contract["params"]["doc_sha256"]["constraints"]["pattern"] == "[0-9a-f]{64}"
    
    # Check success keys
    assert doc_contract["success"]["status"] == 200
    expected_success_keys = ["doc_sha256", "url", "title", "lang", "text", "extracted_at", "provenance"]
    assert doc_contract["success"]["keys"] == expected_success_keys
    
    # Check provenance nested structure
    provenance_keys = doc_contract["success"]["nested"]["provenance"]["keys"]
    expected_provenance_keys = ["url", "fetched_at", "status", "sha256", "robots_allowed"]
    assert provenance_keys == expected_provenance_keys
    
    # Check errors
    error_codes = [err["error"] for err in doc_contract["errors"]]
    assert "invalid_sha256" in error_codes
    assert "not_found" in error_codes


def test_contract_is_json_serializable():
    """Test that the contract contains only JSON-compatible values."""
    # This will fail if there are non-JSON-compatible types
    json.dumps(AGENT_API_CONTRACT)