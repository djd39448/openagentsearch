import json
import hashlib
from pathlib import Path

import pytest
from openagentsearch.extract.store import ExtractStore


def test_extract_store_success(tmp_path: Path) -> None:
    """Test successful extraction storage with valid provenance."""
    # Create a mock raw HTML file 
    raw_content = b"<html><body>Some content</body></html>"
    sha256 = hashlib.sha256(raw_content).hexdigest()
    
    # Write the raw file
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / f"{sha256}.html"
    raw_file.write_bytes(raw_content)
    
    # Write provenance line
    provenance_file = raw_dir / "provenance.jsonl"
    provenance_entry = {
        "url": "https://example.com/page",
        "fetched_at": 1234567890.0,
        "status": 200,
        "sha256": sha256,
        "robots_allowed": True
    }
    provenance_file.write_text(json.dumps(provenance_entry) + "\n")
    
    # Create store and extract data
    store = ExtractStore(tmp_path)
    extracted_data = {
        "text": "Some content",
        "title": "Example Page", 
        "lang": "en"
    }
    
    # Store the extracted document
    file_path = store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    
    # Verify the resulting JSON file
    assert file_path.exists()
    assert file_path.name == f"{sha256}.json"
    
    with open(file_path) as f:
        result = json.load(f)
    
    expected_keys = {"url", "extracted_at", "raw_sha256", "text", "title", "lang"}
    actual_keys = set(result.keys())
    assert actual_keys == expected_keys
    
    assert result["url"] == "https://example.com/page"
    assert result["extracted_at"] == 1234567891.0
    assert result["raw_sha256"] == sha256
    assert result["text"] == "Some content"  
    assert result["title"] == "Example Page"
    assert result["lang"] == "en"


def test_extract_store_raw_hash_mismatch(tmp_path: Path) -> None:
    """Test that a raw file with wrong hash raises ValueError and writes nothing."""
    # Create a mock raw HTML file with one content
    raw_content = b"<html><body>Some content</body></html>"
    sha256 = hashlib.sha256(raw_content).hexdigest()
    
    # Write the raw file (but with different content for mismatch test)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / f"{sha256}.html" 
    # Write different content to test hash mismatch
    raw_file.write_bytes(b"<html><body>Different content</body></html>")
    
    # Write provenance line that matches the original hash (but wrong file)
    provenance_file = raw_dir / "provenance.jsonl"
    provenance_entry = {
        "url": "https://example.com/page",
        "fetched_at": 1234567890.0,
        "status": 200, 
        "sha256": sha256,
        "robots_allowed": True
    }
    provenance_file.write_text(json.dumps(provenance_entry) + "\n")
    
    # Create store and extract data
    store = ExtractStore(tmp_path)
    extracted_data = {
        "text": "Some content",
        "title": "Example Page",
        "lang": "en"
    }
    
    # This should raise ValueError due to hash mismatch
    with pytest.raises(ValueError, match="Raw hash mismatch"):
        store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    
    # Verify no output file was created
    extracted_dir = tmp_path / "extracted"
    assert not extracted_dir.exists() or len(list(extracted_dir.glob("*.json"))) == 0


def test_extract_store_missing_provenance(tmp_path: Path) -> None:
    """Test that missing provenance raises ValueError and writes nothing."""
    # Create a mock raw HTML file
    raw_content = b"<html><body>Some content</body></html>"
    sha256 = hashlib.sha256(raw_content).hexdigest()
    
    # Write the raw file
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / f"{sha256}.html"
    raw_file.write_bytes(raw_content)
    
    # Do NOT create provenance file
    
    # Create store and extract data
    store = ExtractStore(tmp_path)
    extracted_data = {
        "text": "Some content",
        "title": "Example Page",
        "lang": "en"
    }
    
    # This should raise ValueError due to missing provenance 
    with pytest.raises(ValueError, match="No provenance"):
        store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    
    # Verify no output file was created
    extracted_dir = tmp_path / "extracted"
    assert not extracted_dir.exists() or len(list(extracted_dir.glob("*.json"))) == 0


def test_extract_store_url_mismatch(tmp_path: Path) -> None:
    """Test that mismatched URL in provenance raises ValueError and writes nothing."""
    # Create a mock raw HTML file
    raw_content = b"<html><body>Some content</body></html>"
    sha256 = hashlib.sha256(raw_content).hexdigest()
    
    # Write the raw file
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / f"{sha256}.html"
    raw_file.write_bytes(raw_content)
    
    # Write provenance with different URL than what we expect
    provenance_file = raw_dir / "provenance.jsonl" 
    provenance_entry = {
        "url": "https://example.com/different-page",  # Different URL
        "fetched_at": 1234567890.0,
        "status": 200,
        "sha256": sha256,
        "robots_allowed": True
    }
    provenance_file.write_text(json.dumps(provenance_entry) + "\n")
    
    # Create store and extract data  
    store = ExtractStore(tmp_path)
    extracted_data = {
        "text": "Some content",
        "title": "Example Page", 
        "lang": "en"
    }
    
    # This should raise ValueError due to URL mismatch
    with pytest.raises(ValueError, match="URL mismatch"):
        store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    
    # Verify no output file was created
    extracted_dir = tmp_path / "extracted"
    assert not extracted_dir.exists() or len(list(extracted_dir.glob("*.json"))) == 0