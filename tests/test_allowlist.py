import pytest
from src.openagentsearch.fetch.allowlist import load_allowlist, AllowlistEntry


def test_load_allowlist_valid():
    """Test that we can load a valid allowlist."""
    entries = load_allowlist("config/allowlist.yaml")
    
    assert len(entries) == 1
    assert entries[0].host == "docs.python.org"
    assert entries[0].max_pages == 50


def test_load_allowlist_missing_field():
    """Test that loading with missing max_pages field raises ValueError."""
    # Create a temporary invalid file for testing
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("docs.python.org:\n  # max_pages is missing\n")
        temp_file = f.name
    
    try:
        with pytest.raises(ValueError):
            load_allowlist(temp_file)
    finally:
        os.unlink(temp_file)


def test_load_allowlist_wrong_type():
    """Test that loading with wrong types raises ValueError."""
    # Create a temporary invalid file for testing
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("docs.python.org:\n  max_pages: \"not_an_integer\"\n")
        temp_file = f.name
    
    try:
        with pytest.raises(ValueError):
            load_allowlist(temp_file)
    finally:
        os.unlink(temp_file)


def test_load_allowlist_structure():
    """Test that loading with wrong structure raises ValueError."""
    # Create a temporary invalid file for testing
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("not_a_dict\n")
        temp_file = f.name
    
    try:
        with pytest.raises(ValueError):
            load_allowlist(temp_file)
    finally:
        os.unlink(temp_file)