import json
import os
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from openagentsearch.api.doc import make_doc_route
from openagentsearch.api.server import create_server


def test_provenance_line_skipped_but_valid_entry_still_returned():
    """Test that a malformed unrelated line in provenance.jsonl is skipped but valid matching entry is still returned."""
    # Create a mock provenance file with one malformed unrelated line followed by a valid matching line
    provenance_lines = [
        '{"url": "http://example.com/other", "fetched_at": 1234567890, "status": 200, "sha256": "abc123def456", "robots_allowed": true}\n',  # malformed unrelated line
        '{"url": "http://example.com/page", "fetched_at": 1234567890, "status": 200, "sha256": "abc123def456", "robots_allowed": true}\n',  # valid matching line
    ]
    
    # Create the temporary directory structure
    temp_root = Path("C:/Users/trustcore-rdp/agentsearch-hermes/temp_work")
    raw_dir = temp_root / "raw"
    extracted_dir = temp_root / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Create an extracted document
    doc_sha256 = "abc123def456" * 8  # 64 character hex string
    extracted_file = extracted_dir / f"{doc_sha256}.json"
    extracted_content = {
        "url": "http://example.com/page",
        "title": "Test Page",
        "lang": "en",
        "text": "This is test content.",
        "extracted_at": 1234567890,
    }
    
    with open(extracted_file, "w") as f:
        json.dump(extracted_content, f)
    
    # Create provenance.jsonl
    provenance_file = raw_dir / "provenance.jsonl"
    with open(provenance_file, "w") as f:
        for line in provenance_lines:
            f.write(line)
            
    # Test the doc route function directly
    doc_route = make_doc_route(temp_root)
    
    # Make a direct call to the route handler
    status, response = doc_route(doc_sha256, {})
    
    assert status == 200
    # Validate that it still finds and returns the valid matching provenance entry
    assert "provenance" in response
    assert response["provenance"]["url"] == "http://example.com/page"
    assert response["provenance"]["status"] == 200


def test_malformed_matching_provenance_entry_raises_value_error():
    """Test that a matching provenance line missing a required field raises ValueError with KeyError as cause."""
    # Create the temporary directory structure
    temp_root = Path("C:/Users/trustcore-rdp/agentsearch-hermes/temp_work")
    raw_dir = temp_root / "raw"
    extracted_dir = temp_root / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Create an extracted document
    doc_sha256 = "abc123def456" * 8  # 64 character hex string
    extracted_file = extracted_dir / f"{doc_sha256}.json"
    extracted_content = {
        "url": "http://example.com/page",
        "title": "Test Page",
        "lang": "en",
        "text": "This is test content.",
        "extracted_at": 1234567890,
    }
    
    with open(extracted_file, "w") as f:
        json.dump(extracted_content, f)
    
    # Create provenance.jsonl with a matching malformed line (missing one required field)
    provenance_file = raw_dir / "provenance.jsonl"
    with open(provenance_file, "w") as f:
        # This is missing a required field 'status'
        f.write('{"url": "http://example.com/page", "fetched_at": 1234567890, "sha256": "abc123def456", "robots_allowed": true}\n')
    
    # Test the doc route function directly
    doc_route = make_doc_route(temp_root)
    
    # Make a direct call to the route handler which should raise ValueError with KeyError as cause
    with pytest.raises(ValueError) as exc_info:
        doc_route(doc_sha256, {})
    
    assert "Malformed matching provenance entry" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_error_reading_provenance_raises_value_error():
    """Test that if opening or reading raw/provenance.jsonl raises OSError/UnicodeError, ValueError is raised with proper chaining."""
    # Create the temporary directory structure
    temp_root = Path("C:/Users/trustcore-rdp/agentsearch-hermes/temp_work")
    extracted_dir = temp_root / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    
    # Create an extracted document
    doc_sha256 = "abc123def456" * 8  # 64 character hex string
    extracted_file = extracted_dir / f"{doc_sha256}.json"
    extracted_content = {
        "url": "http://example.com/page",
        "title": "Test Page",
        "lang": "en",
        "text": "This is test content.",
        "extracted_at": 1234567890,
    }
    
    with open(extracted_file, "w") as f:
        json.dump(extracted_content, f)
    
    # Test that if reading provenance.jsonl fails with OSError, it raises a proper ValueError
    def mock_open_with_oserror(*args, **kwargs):
        raise OSError("File not found")
    
    # Test the doc route function directly
    doc_route = make_doc_route(temp_root)
    
    # Temporarily replace open with our mock that raises OSError for provenance file
    with patch('builtins.open', side_effect=mock_open_with_oserror) as mocked_open:
        # This should raise ValueError with OSError as cause when trying to open provenance file
        with pytest.raises(ValueError) as exc_info:
            doc_route(doc_sha256, {})
        
        assert "Error reading provenance" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)
    
    
def test_error_reading_extracted_document_raises_value_error():
    """Test that if extracting an extracted document raises OSError/UnicodeError, ValueError is raised with proper chaining."""
    # Create the temporary directory structure
    temp_root = Path("C:/Users/trustcore-rdp/agentsearch-hermes/temp_work")
    raw_dir = temp_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Create an extracted document path but make it unreadable (simulated by not creating it correctly)
    doc_sha256 = "abc123def456" * 8  # 64 character hex string
    
    # Test the doc route function directly
    doc_route = make_doc_route(temp_root)
    
    # Temporarily replace open with our mock that raises OSError when trying to read extracted document 
    def mock_open_extracted(*args, **kwargs):
        raise OSError("Document not found")
    
    with patch('builtins.open', side_effect=mock_open_extracted) as mocked_open:
        # This should raise ValueError when trying to read the extracted document
        with pytest.raises(ValueError) as exc_info:
            doc_route(doc_sha256, {})
        
        assert "Error reading extracted document" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)