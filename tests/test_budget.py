import json
import pathlib
from unittest.mock import patch

import pytest

from openagentsearch.fetch.budget import PageBudget


def test_allow_within_budget(tmp_path):
    """Test that allow() returns True while under budget."""
    budget = PageBudget(tmp_path, {"example.com": 2})
    
    # First two calls should be allowed
    assert budget.allow("example.com") is True
    assert budget.allow("example.com") is True
    
    # Third call should be denied
    assert budget.allow("example.com") is False


def test_stop_marker_written_once(tmp_path):
    """Test that the STOP marker file is written only once when budget is exceeded."""
    budget = PageBudget(tmp_path, {"example.com": 2})
    
    # Allow two calls (within budget)
    assert budget.allow("example.com") is True
    assert budget.allow("example.com") is True
    
    # Third call should deny and create marker
    assert budget.allow("example.com") is False
    marker_path = tmp_path / "STOP-example.com"
    assert marker_path.exists()
    
    # Read the marker content
    marker_content = marker_path.read_text()
    expected_marker = {"host": "example.com", "max_pages": 2, "reason": "max_pages reached"}
    assert json.loads(marker_content) == expected_marker
    
    # Fourth call should still deny but not rewrite marker
    assert budget.allow("example.com") is False
    # Content should be unchanged
    assert json.loads(marker_path.read_text()) == expected_marker


def test_unlisted_host_always_denied(tmp_path):
    """Test that hosts not in the allowlist are always denied."""
    budget = PageBudget(tmp_path, {"example.com": 2})
    
    # Should never be allowed for unlisted host
    assert budget.allow("unlisted.com") is False
    assert budget.allow("another.com") is False
    
    # No markers should be written
    assert not (tmp_path / "STOP-unlisted.com").exists()
    assert not (tmp_path / "STOP-another.com").exists()


def test_multiple_hosts_count_independently(tmp_path):
    """Test that different hosts are tracked independently."""
    budget = PageBudget(tmp_path, {"host1.com": 2, "host2.com": 3})
    
    # Host1: allow 2 times
    assert budget.allow("host1.com") is True
    assert budget.allow("host1.com") is True
    
    # Host2: allow 3 times (within limit)
    assert budget.allow("host2.com") is True
    assert budget.allow("host2.com") is True
    assert budget.allow("host2.com") is True
    
    # Host2: should be denied now
    assert budget.allow("host2.com") is False
    
    # Host1: should still be allowed (3rd time is denied)
    assert budget.allow("host1.com") is False
    
    # Check that both have markers
    assert (tmp_path / "STOP-host1.com").exists()
    assert (tmp_path / "STOP-host2.com").exists()
    
    # Marker content validation
    marker1_content = json.loads((tmp_path / "STOP-host1.com").read_text())
    marker2_content = json.loads((tmp_path / "STOP-host2.com").read_text())
    
    assert marker1_content == {"host": "host1.com", "max_pages": 2, "reason": "max_pages reached"}
    assert marker2_content == {"host": "host2.com", "max_pages": 3, "reason": "max_pages reached"}