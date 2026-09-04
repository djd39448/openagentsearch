"""Tests for the fetch policy module integration with robots.txt."""

import pytest
from openagentsearch.fetch.policy import robots_allows


@pytest.fixture
def sample_robots_txt():
    return """User-agent: *
Disallow: /private/
Allow: /
"""


@pytest.fixture
def sample_robots_txt_named_agent():
    return """User-agent: openagentsearch
Disallow: /private/
Allow: /

User-agent: *
Disallow: /
"""


def test_non_allowlisted_host_returns_false_even_with_permissive_robots(sample_robots_txt):
    """Test that a non-allowlisted host returns False even when robots.txt allows everything."""
    assert robots_allows(
        "https://example.com/public/page",
        allowlist=["docs.python.org"],  # docs.python.org is the only allowed host
        robots_txt=sample_robots_txt
    ) is False


def test_allowlisted_host_with_disallowed_path_returns_false(sample_robots_txt_named_agent):
    """Test that an allowlisted URL returns False when robots.txt disallows its path."""
    assert robots_allows(
        "https://docs.python.org/private/item",
        allowlist=["docs.python.org"],
        robots_txt=sample_robots_txt_named_agent
    ) is False


def test_allowlisted_host_with_allowed_path_returns_true(sample_robots_txt_named_agent):
    """Test that an allowlisted URL returns True when robots.txt allows its path."""
    assert robots_allows(
        "https://docs.python.org/public/page",
        allowlist=["docs.python.org"],
        robots_txt=sample_robots_txt_named_agent
    ) is True


def test_allowlisted_host_with_no_robots_txt_returns_true():
    """Test that the same allowlisted URL with no robots text returns True (legacy behavior)."""
    assert robots_allows(
        "https://docs.python.org/public/page",
        allowlist=["docs.python.org"]
    ) is True