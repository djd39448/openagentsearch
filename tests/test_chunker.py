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
    
    # Should produce exactly 4 chunks
    assert len(result) == 4
    
    # Check each chunk's content and index
    assert result[0]["chunk_index"] == 0
    assert result[0]["text"] == "This is a "
    assert result[0]["doc_sha256"] == doc_sha256
    
    assert result[1]["chunk_index"] == 1
    assert result[1]["text"] == "is a test"
    assert result[1]["doc_sha256"] == doc_sha256
    
    assert result[2]["chunk_index"] == 2
    assert result[2]["text"] == "a test se"
    assert result[2]["doc_sha256"] == doc_sha256
    
    assert result[3]["chunk_index"] == 3
    assert result[3]["text"] == "test sente"
    assert result[3]["doc_sha256"] == doc_sha256


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
    
    # Reconstruct the original text
    reconstructed = chunks[0]["text"]  # First chunk in full
    for i in range(1, len(chunks)):
        # Remove overlap from subsequent chunks before appending
        start_of_overlap = len(chunks[i]["text"]) - overlap
        reconstructed += chunks[i]["text"][start_of_overlap:]
    
    assert reconstructed == text