"""Tests for the chunk_text function."""

import pytest
from openagentsearch.chunk.chunker import chunk_text


def test_deterministic_chunk_sequence():
    """Test a fixed input produces a deterministic sequence."""
    doc_sha256 = "test_sha256"
    text = "This is a test sentence for chunking."
    chunk_size = 10
    overlap = 3
    
    result = chunk_text(doc_sha256, text, chunk_size, overlap)

    # 37 characters, size 10, overlap 3: starts advance by 7 -> 0, 7, 14, 21, 28.
    # The chunk starting at 28 reaches the end of the text, so it is the last one.
    expected = [
        {"doc_sha256": doc_sha256, "chunk_index": 0, "text": "This is a "},
        {"doc_sha256": doc_sha256, "chunk_index": 1, "text": " a test se"},
        {"doc_sha256": doc_sha256, "chunk_index": 2, "text": " sentence "},
        {"doc_sha256": doc_sha256, "chunk_index": 3, "text": "ce for chu"},
        {"doc_sha256": doc_sha256, "chunk_index": 4, "text": "chunking."},
    ]
    assert result == expected

    # Same input, same output: the function is deterministic.
    assert chunk_text(doc_sha256, text, chunk_size, overlap) == expected


def test_stable_chunk_index_and_doc_hash():
    """Test that chunk indices are stable and doc_sha256 is propagated."""
    doc_sha256 = "another_test_hash_123456"
    text = "A longer example with more text to ensure proper chunking behavior."
    chunk_size = 15
    overlap = 5
    
    result = chunk_text(doc_sha256, text, chunk_size, overlap)
    
    # Verify each chunk has correct index and doc hash
    for i, chunk in enumerate(result):
        assert chunk["chunk_index"] == i
        assert chunk["doc_sha256"] == doc_sha256


def test_final_short_chunk():
    """Test final short chunk behavior."""
    doc_sha256 = "short_test"
    text = "Short"
    chunk_size = 10
    overlap = 3
    
    result = chunk_text(doc_sha256, text, chunk_size, overlap)
    
    # Should produce only one chunk (shorter than chunk_size)
    assert len(result) == 1
    assert result[0]["text"] == "Short"
    assert result[0]["chunk_index"] == 0


def test_empty_input():
    """Test that empty input returns empty list."""
    result = chunk_text("test_hash", "", 10, 3)
    assert result == []


def test_invalid_parameters():
    """Test that invalid parameters raise ValueError."""
    with pytest.raises(ValueError, match="chunk_size must be > 0"):
        chunk_text("test", "text", 0, 3)
        
    with pytest.raises(ValueError, match="overlap must be >= 0"):
        chunk_text("test", "text", 10, -1)
        
    with pytest.raises(ValueError, match="overlap must be < chunk_size"):
        chunk_text("test", "text", 5, 10)


def test_source_coverage_reconstruction():
    """Test that source can be reconstructed from chunks."""
    doc_sha256 = "reconstruct_test"
    text = "This is a test for reconstructing the original text from chunks."
    chunk_size = 8
    overlap = 2
    
    chunks = chunk_text(doc_sha256, text, chunk_size, overlap)
    
    # Reconstruct the original text: chunk 0 in full, then each later chunk with its
    # LEADING `overlap` characters removed (those repeat the tail of the previous chunk).
    reconstructed = chunks[0]["text"]
    for chunk in chunks[1:]:
        reconstructed += chunk["text"][overlap:]

    assert reconstructed == text
    # Every chunk except the last is exactly chunk_size long; the last may be shorter.
    assert all(len(c["text"]) == chunk_size for c in chunks[:-1])
    assert 0 < len(chunks[-1]["text"]) <= chunk_size