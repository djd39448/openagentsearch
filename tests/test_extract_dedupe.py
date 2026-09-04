import hashlib
import json
from pathlib import Path

import pytest
from openagentsearch.extract.dedupe import DedupingExtractStore


def test_dedupe_store_same_document_twice(tmp_path: Path) -> None:
    """Test that storing the same document twice yields exactly ONE extracted JSON file 
    and the second call returns None.
    """
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
    store = DedupingExtractStore(tmp_path)
    extracted_data = {
        "text": "Some content",
        "title": "Example Page", 
        "lang": "en"
    }
    
    # First put should succeed and return a file path
    file_path1 = store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    assert file_path1 is not None
    assert file_path1.exists()
    
    # Second put with identical data should return None (deduplicated)
    file_path2 = store.put(sha256, "https://example.com/page", extracted_data, 1234567892.0)
    assert file_path2 is None
    
    # Verify only one extracted file exists
    extracted_dir = tmp_path / "extracted"
    assert len(list(extracted_dir.glob("*.json"))) == 1


def test_dedupe_store_different_documents_same_text(tmp_path: Path) -> None:
    """Test that two DIFFERENT raw documents (different sha256 + provenance) with 
    IDENTICAL extracted text yield exactly one record.
    """
    # Create two different raw HTML files with identical content
    raw_content1 = b"<html><body>Same content</body></html>"
    raw_content2 = b"<html><body>Same content</body></html>"  # Same content
    sha256_1 = hashlib.sha256(raw_content1).hexdigest()
    sha256_2 = hashlib.sha256(raw_content2).hexdigest()
    
    # Both should have the same text hash but different raw hashes
    
    # Write both raw files
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    
    raw_file1 = raw_dir / f"{sha256_1}.html"
    raw_file1.write_bytes(raw_content1)
    
    raw_file2 = raw_dir / f"{sha256_2}.html"
    raw_file2.write_bytes(raw_content2)
    
    # Write provenance lines
    provenance_file = raw_dir / "provenance.jsonl"
    
    provenance_entry1 = {
        "url": "https://example.com/page1",
        "fetched_at": 1234567890.0,
        "status": 200,
        "sha256": sha256_1,
        "robots_allowed": True
    }
    
    provenance_entry2 = {
        "url": "https://example.com/page2",  # Different URL 
        "fetched_at": 1234567891.0,
        "status": 200,
        "sha256": sha256_2,
        "robots_allowed": True
    }
    
    provenance_file.write_text(json.dumps(provenance_entry1) + "\n" + json.dumps(provenance_entry2) + "\n")
    
    # Create store and extract data (both have same extracted text)
    store = DedupingExtractStore(tmp_path)
    extracted_data1 = {
        "text": "Same content",
        "title": "Page 1", 
        "lang": "en"
    }
    extracted_data2 = {
        "text": "Same content",
        "title": "Page 2",
        "lang": "fr"
    }
    
    # First put should succeed
    file_path1 = store.put(sha256_1, "https://example.com/page1", extracted_data1, 1234567892.0)
    assert file_path1 is not None
    
    # Second put with different raw but same text should return None (deduplicated)
    file_path2 = store.put(sha256_2, "https://example.com/page2", extracted_data2, 1234567893.0)
    assert file_path2 is None
    
    # Verify only one extracted file exists
    extracted_dir = tmp_path / "extracted"
    assert len(list(extracted_dir.glob("*.json"))) == 1


def test_dedupe_store_different_text(tmp_path: Path) -> None:
    """Test that different extracted text yields two records."""
    # Create two raw HTML files with different content
    raw_content1 = b"<html><body>Content 1</body></html>"
    raw_content2 = b"<html><body>Content 2</body></html>"
    sha256_1 = hashlib.sha256(raw_content1).hexdigest()
    sha256_2 = hashlib.sha256(raw_content2).hexdigest()

    # Write both raw files
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    
    raw_file1 = raw_dir / f"{sha256_1}.html"
    raw_file1.write_bytes(raw_content1)
    
    raw_file2 = raw_dir / f"{sha256_2}.html"
    raw_file2.write_bytes(raw_content2)
    
    # Write provenance lines
    provenance_file = raw_dir / "provenance.jsonl"
    
    provenance_entry1 = {
        "url": "https://example.com/page1",
        "fetched_at": 1234567890.0,
        "status": 200,
        "sha256": sha256_1,
        "robots_allowed": True
    }
    
    provenance_entry2 = {
        "url": "https://example.com/page2",  
        "fetched_at": 1234567891.0,
        "status": 200,
        "sha256": sha256_2,
        "robots_allowed": True
    }
    
    provenance_file.write_text(json.dumps(provenance_entry1) + "\n" + json.dumps(provenance_entry2) + "\n")
    
    # Create store and extract data (different extracted text)
    store = DedupingExtractStore(tmp_path)
    extracted_data1 = {
        "text": "Content 1",
        "title": "Page 1", 
        "lang": "en"
    }
    extracted_data2 = {
        "text": "Content 2",  # Different text
        "title": "Page 2",
        "lang": "fr"
    }
    
    # Both puts should succeed with different results
    file_path1 = store.put(sha256_1, "https://example.com/page1", extracted_data1, 1234567892.0)
    assert file_path1 is not None
    
    file_path2 = store.put(sha256_2, "https://example.com/page2", extracted_data2, 1234567893.0)  
    assert file_path2 is not None
    
    # Verify two extracted files exist
    extracted_dir = tmp_path / "extracted"
    assert len(list(extracted_dir.glob("*.json"))) == 2


def test_dedupe_store_non_ascii_text(tmp_path: Path) -> None:
    """Test that non-ASCII text is written and read back as UTF-8 intact, 
    and a second put with the same text dedupes.
    """
    # Create a raw HTML file
    raw_content = b"<html><body>cafe u Japanese</body></html>"
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
    store = DedupingExtractStore(tmp_path)
    extracted_data = {
        "text": "cafe ü 日本",
        "title": "Example Page", 
        "lang": "en"
    }
    
    # First put should succeed and return a file path
    file_path1 = store.put(sha256, "https://example.com/page", extracted_data, 1234567891.0)
    assert file_path1 is not None
    assert file_path1.exists()
    
    # Read back the stored content to verify UTF-8 handling
    with open(file_path1, 'r', encoding='utf-8') as f:
        stored_data = json.load(f)
    assert stored_data["text"] == "cafe ü 日本"
    
    # Second put with identical text should return None (deduplicated)
    file_path2 = store.put(sha256, "https://example.com/page", extracted_data, 1234567892.0)
    assert file_path2 is None
    
    # Verify only one extracted file exists
    extracted_dir = tmp_path / "extracted"
    assert len(list(extracted_dir.glob("*.json"))) == 1