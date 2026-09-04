"""Deterministic overlapping text chunking implementation."""

from typing import List, Dict, Any


def chunk_text(doc_sha256: str, text: str, chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
    """
    Chunk text into overlapping pieces of specified size.
    
    Args:
        doc_sha256: The SHA256 hash of the document
        text: The text to chunk
        chunk_size: Size of each chunk (must be > 0)
        overlap: Number of characters to overlap between chunks (must be >= 0, < chunk_size)
        
    Returns:
        List of chunk dictionaries with keys: doc_sha256, chunk_index, text
        
    Raises:
        ValueError: If parameters are invalid
    """
    # Validate inputs
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if overlap >= chunk_size:
        raise ValueError("overlap must be < chunk_size")
    
    # Handle empty input
    if not text:
        return []
    
    chunks = []
    start = 0
    index = 0
    
    # Generate chunks until we reach the end of the text
    while True:
        # Calculate end position for this chunk (this ensures we don't go past text end)
        current_chunk_end = min(start + chunk_size, len(text))
        
        # Extract the chunk text
        chunk_text = text[start:current_chunk_end]
        
        # Append to results
        chunks.append({
            "doc_sha256": doc_sha256,
            "chunk_index": index,
            "text": chunk_text
        })
        
        # Early exit if we've reached the end of the text or are done with the loop
        if current_chunk_end >= len(text):
            break
            
        # Calculate next start position (advance by overlap characters)
        start = start + chunk_size - overlap
        index += 1
    
    return chunks