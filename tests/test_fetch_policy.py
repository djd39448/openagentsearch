"""Tests for the fetch policy module."""

import pytest
from openagentsearch.fetch.policy import robots_allows


def test_allowlisted_url_passes() -> None:
    """Test that allowlisted URLs pass the policy check."""
    assert robots_allows("https://docs.python.org/3/tutorial/index.html")
    assert robots_allows("http://docs.python.org/2.7/")
    assert robots_allows("https://docs.python.org/")


def test_non_allowlisted_url_fails() -> None:
    """Test that non-allowlisted URLs fail the policy check."""
    assert not robots_allows("https://example.com/")
    assert not robots_allows("http://google.com/search")
    assert not robots_allows("https://github.com/user/repo")


def test_different_scheme_and_port() -> None:
    """Test that different schemes and ports are handled correctly."""
    assert robots_allows("http://docs.python.org:80/3/")
    assert robots_allows("https://docs.python.org:443/3/")
    assert not robots_allows("http://example.com:8080/")