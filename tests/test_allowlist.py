import tempfile
import os
from openagentsearch.fetch.allowlist import load_allowlist, AllowlistEntry

def test_load_allowlist():
    # Create a temporary allowlist file
    allowlist_content = """
docs.python.org:
  max_pages: 50
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(allowlist_content)
        temp_path = f.name
    
    try:
        # Load the allowlist
        entries = load_allowlist(temp_path)
        
        # Verify the entry
        assert len(entries) == 1
        entry = entries[0]
        assert isinstance(entry, AllowlistEntry)
        assert entry.host == "docs.python.org"
        assert entry.max_pages == 50
        
        # Test that it raises ValueError for malformed config
        malformed_content = """
docs.python.org:
  max_pages: not_an_int
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(malformed_content)
            malformed_path = f.name
        
        try:
            load_allowlist(malformed_path)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected
        finally:
            os.unlink(malformed_path)
            
    finally:
        os.unlink(temp_path)