import json
import tempfile
from pathlib import Path
from typing import Dict, Any

import pytest

from openagentsearch.extract.dedupe import DedupingExtractStore


def test_deduping_store_stores_first_document(tmp_path: Path):
    """Test that the first document is stored correctly."""
    # Create a DedupingExtractStore instance
    store = DedupingExtractStore(tmp_path)
    
    # Define test data
    sha256 = "a" * 64  # A fake SHA256 hash
    url = "https://example.com/test1"
    extracted = {
        "text": "This is some test text content.",
        "title": "Test Title",
        "lang": "en"
    }
    extracted_at = 12345.0
    
    # Store the document and verify the path
    result_path = store.put(sha256, url, extracted, extracted_at)
    assert result_path is not None
    assert result_path.exists()
    
    # Verify content of stored file
    with result_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    
    expected_data = {
        "url": url,
        "extracted_at": extracted_at,
        "raw_sha256": sha256,
        "text": extracted["text"],
        "title": extracted["title"],
        "lang": extracted["lang"]
    }
    assert data == expected_data


def test_deduping_store_returns_none_for_duplicate_text(tmp_path: Path):
    """Test that duplicate documents are not stored."""
    # Create a DedupingExtractStore instance
    store = DedupingExtractStore(tmp_path)
    
    # Define test data for first document
    sha256 = "a" * 64  # A fake SHA256 hash
    url = "https://example.com/test1"
    extracted = {
        "text": "This is some test text content.",
        "title": "Test Title",
        "lang": "en"
    }
    extracted_at = 12345.0
    
    # Store the first document
    result_path = store.put(sha256, url, extracted, extracted_at)
    assert result_path is not None
    
    # Define test data for duplicate document (same text content)
    sha256_2 = "b" * 64  # Different SHA256 hash
    url_2 = "https://example.com/test2"
    extracted_2 = {
        "text": "This is some test text content.",  # Same text
        "title": "Different Title",  # Different title 
        "lang": "fr"
    }
    
    # Try to store the duplicate and verify None is returned
    result_path_2 = store.put(sha256_2, url_2, extracted_2, extracted_at + 1)
    assert result_path_2 is None
    
    # Verify only one file was created
    extracted_dir = tmp_path / "extracted"
    files = list(extracted_dir.iterdir())
    assert len(files) == 1


def test_deduping_store_stores_different_text(tmp_path: Path):
    """Test that documents with different text content are stored."""
    # Create a DedupingExtractStore instance
    store = DedupingExtractStore(tmp_path)
    
    # Define test data for first document
    sha256 = "a" * 64  # A fake SHA256 hash
    url = "https://example.com/test1"
    extracted = {
        "text": "This is some test text content.",
        "title": "Test Title",
        "lang": "en"
    }
    extracted_at = 12345.0
    
    # Store the first document
    result_path = store.put(sha256, url, extracted, extracted_at)
    assert result_path is not None
    
    # Define test data for different document (different text)
    sha256_2 = "b" * 64  # Different SHA256 hash
    url_2 = "https://example.com/test2"
    extracted_2 = {
        "text": "This is different content.",  # Different text
        "title": "Different Title",
        "lang": "fr"
    }
    
    # Store the second document and verify path is returned
    result_path_2 = store.put(sha256_2, url_2, extracted_2, extracted_at + 1)
    assert result_path_2 is not None
    
    # Verify two files were created 
    extracted_dir = tmp_path / "extracted"
    files = list(extracted_dir.iterdir())
    assert len(files) == 2


def test_deduping_store_handles_unicode_text(tmp_path: Path):
    """Test that Unicode text is handled correctly for deduplication."""
    # Create a DedupingExtractStore instance
    store = DedupingExtractStore(tmp_path)
    
    # Define test data with Unicode characters
    sha256 = "a" * 64
    url = "https://example.com/test_unicode"
    extracted = {
        "text": "café ü 日本",
        "title": "Unicode Test",
        "lang": "en"
    }
    extracted_at = 12345.0
    
    # Store the first document
    result_path = store.put(sha256, url, extracted, extracted_at)
    assert result_path is not None
    
    # Define duplicate text with Unicode characters
    sha256_2 = "b" * 64
    url_2 = "https://example.com/test_unicode_duplicate"
    extracted_2 = {
        "text": "café ü 日本",  # Same Unicode text
        "title": "Different Title",
        "lang": "fr"
    }
    
    # Try to store the duplicate and verify None is returned
    result_path_2 = store.put(sha256_2, url_2, extracted_2, extracted_at + 1)
    assert result_path_2 is None
    
    # Verify only one file was created
    extracted_dir = tmp_path / "extracted"
    files = list(extracted_dir.iterdir())
    assert len(files) == 1