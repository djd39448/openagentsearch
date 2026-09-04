import tempfile
from pathlib import Path

# Import the class directly for testing
from openagentsearch.vector.store import VectorStore


def test_add_three_vectors():
    """Test adding three vectors, checking count, get and load_all contents with expected dimension"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        store = VectorStore(store_path, 3)
        
        # Add three vectors
        store.add("chunk1", "doc1", [1.0, 2.0, 3.0], "text1")
        store.add("chunk2", "doc1", [4.0, 5.0, 6.0], "text2") 
        store.add("chunk3", "doc2", [7.0, 8.0, 9.0], "text3")
        
        # Check count
        assert store.count() == 3
        
        # Check get
        result1 = store.get("chunk1")
        assert result1 is not None
        assert result1["chunk_id"] == "chunk1"
        assert result1["doc_sha256"] == "doc1"
        assert result1["vector"] == [1.0, 2.0, 3.0]
        assert result1["text"] == "text1"
        
        # Check load_all ordering
        all_results = store.load_all()
        assert len(all_results) == 3
        assert all_results[0]["chunk_id"] == "chunk1"
        assert all_results[1]["chunk_id"] == "chunk2" 
        assert all_results[2]["chunk_id"] == "chunk3"
        
        # Verify dimensions are preserved
        for item in all_results:
            assert len(item["vector"]) == 3
            
        # Close and reopen to test persistence
        store.close()
        store2 = VectorStore(store_path, 3)
        
        # Check that all persisted correctly
        assert store2.count() == 3
        result1_reloaded = store2.get("chunk1")
        assert result1_reloaded is not None
        assert result1_reloaded["vector"] == [1.0, 2.0, 3.0]
        
        # Check load_all after reload
        all_results_reloaded = store2.load_all()
        assert len(all_results_reloaded) == 3
        store2.close()


def test_duplicate_chunk_id_raises_value_error():
    """Test that duplicate chunk_id raises ValueError and original record and count unchanged"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        store = VectorStore(store_path, 2)
        
        # Add initial vector
        store.add("chunk1", "doc1", [1.0, 2.0], "text1")
        assert store.count() == 1
        
        # Try to add duplicate chunk_id
        try:
            store.add("chunk1", "doc2", [3.0, 4.0], "text2")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "already exists" in str(e)
            
        # Original record should still be there and count unchanged
        assert store.count() == 1
        result = store.get("chunk1")
        assert result is not None
        assert result["text"] == "text1"
        assert result["vector"] == [1.0, 2.0]
        store.close()


def test_wrong_length_vector_raises_value_error():
    """Test that a wrong-length vector raises ValueError and nothing gets written"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        store = VectorStore(store_path, 3)
        
        # Try to add vector with wrong length
        try:
            store.add("chunk1", "doc1", [1.0, 2.0], "text1")  # Only 2 elements instead of 3
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "length" in str(e) or "dimension" in str(e)
        
        # No records should be added
        assert store.count() == 0
        store.close()


def test_non_numeric_and_boolean_elements_raise_value_error():
    """Test that non-numeric elements and booleans raise ValueError"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        store = VectorStore(store_path, 3)
        
        # Test boolean element
        try:
            store.add("chunk1", "doc1", [1.0, True, 3.0], "test")
            assert False, "Should have raised ValueError for boolean"
        except ValueError as e:
            assert "boolean" in str(e)
            
        # Test string element
        try:
            store.add("chunk2", "doc2", [1.0, "hello", 3.0], "test") 
            assert False, "Should have raised ValueError for non-numeric"
        except ValueError as e:
            assert "not numeric" in str(e)
            
        # Test None element
        try:
            store.add("chunk3", "doc3", [1.0, None, 3.0], "test")
            assert False, "Should have raised ValueError for non-numeric"
        except ValueError as e:
            assert "not numeric" in str(e)

        # Nothing was written by any of the rejected calls
        assert store.count() == 0
        store.close()


def test_load_all_ordering_deterministic():
    """Test that load_all ordering is deterministic by chunk_id regardless of insertion order"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        store = VectorStore(store_path, 2)
        
        # Insert in non-alphabetical order
        store.add("chunk3", "doc1", [3.0, 4.0], "text3")
        store.add("chunk1", "doc2", [1.0, 2.0], "text1")
        store.add("chunk2", "doc3", [5.0, 6.0], "text2")
        
        # Should be ordered by chunk_id even though we inserted differently
        all_results = store.load_all()
        assert len(all_results) == 3
        expected_ids = ["chunk1", "chunk2", "chunk3"]
        actual_ids = [item["chunk_id"] for item in all_results]
        assert actual_ids == expected_ids
        store.close()


def test_reopen_different_dimension_raises_value_error():
    """Test that reopening the same database with a different configured dimension raises ValueError"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "vectors.sqlite3"
        
        # Create store and add record
        store1 = VectorStore(store_path, 3)
        store1.add("chunk1", "doc1", [1.0, 2.0, 3.0], "text1")
        assert store1.count() == 1
        
        # Close the first instance
        store1.close()
        
        # Try to reopen with different dimension - should raise an error when accessing data
        store2 = VectorStore(store_path, 4)  # Different dimension
        
        # This should trigger ValueError when we try to access the existing records 
        try:
            result = store2.get("chunk1")
            assert False, "Should have raised ValueError on dimension mismatch"
        except ValueError as e:
            assert "dimension" in str(e)
        store2.close()