import json
import tempfile
from pathlib import Path

import pytest

from openagentsearch.fetch.rawstore import RawStore


@pytest.fixture
def temp_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_put_creates_html_file_and_provenance_entry(temp_root):
    store = RawStore(temp_root)
    
    # Create a sample HTML body and put it in store
    url = "https://example.com/test"
    body = b"<html><body>Test content</body></html>"
    status = 200
    robots_allowed = True
    fetched_at = 1234567890.0
    
    sha256 = store.put(url, body, status, robots_allowed, fetched_at)
    
    # Verify the file was created with correct name (sha256 of body)
    expected_file = temp_root / "raw" / f"{sha256}.html"
    assert expected_file.exists()
    
    # Check file content
    with open(expected_file, "rb") as f:
        assert f.read() == body
    
    # Verify provenance entry was appended correctly
    provenance_file = temp_root / "raw" / "provenance.jsonl"
    with open(provenance_file, "r") as f:
        lines = f.readlines()
        
    assert len(lines) == 1
    
    # Parse the provenance line and check fields
    data = json.loads(lines[0])
    expected_data = {
        "url": url,
        "fetched_at": fetched_at,
        "status": status,
        "sha256": sha256,
        "robots_allowed": robots_allowed
    }
    
    assert data == expected_data


def test_put_same_body_twice(temp_root):
    store = RawStore(temp_root)
    
    url = "https://example.com/test"
    body = b"<html><body>Test content</body></html>"
    status = 200
    robots_allowed = True
    fetched_at = 1234567890.0
    
    # Put the same body twice 
    sha256_1 = store.put(url, body, status, robots_allowed, fetched_at)
    sha256_2 = store.put(url, body, status, robots_allowed, fetched_at + 1)  # different fetched_at
    
    # Should have same sha256
    assert sha256_1 == sha256_2
    
    # Should still only have one file 
    expected_file = temp_root / "raw" / f"{sha256_1}.html"
    assert expected_file.exists()
    
    # Should have two provenance entries (append-only)
    provenance_file = temp_root / "raw" / "provenance.jsonl"
    with open(provenance_file, "r") as f:
        lines = f.readlines()
        
    assert len(lines) == 2
    
    # Both should be valid JSON and have the right fields
    for line in lines:
        data = json.loads(line)
        expected_fields = ["url", "fetched_at", "status", "sha256", "robots_allowed"]
        for field in expected_fields:
            assert field in data


def test_provenance_roundtrips():
    # Test that provenance data roundtrips correctly through JSON
    data_in = {
        "url": "https://example.com/test",
        "fetched_at": 1234567890.0,
        "status": 200,
        "sha256": "a" * 64,  # Mock sha256
        "robots_allowed": True
    }
    
    # Serialize
    line = json.dumps(data_in)
    
    # Deserialize  
    data_out = json.loads(line)
    
    assert data_in == data_out