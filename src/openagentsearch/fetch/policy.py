"""Policy checking for the fetcher module."""

from urllib.parse import urlparse
from typing import List

# Allowlist of domains that are permitted to be fetched
ALLOWLIST: List[str] = ["docs.python.org"]


def robots_allows(url: str, user_agent: str = "OpenAgentSearch-crawler/1.0") -> bool:
    """
    Check if the given URL is allowed by robots.txt policy.
    
    For now, this is a stub implementation that just checks the allowlist.
    In a full implementation, this would fetch and parse robots.txt.
    
    Args:
        url: The URL to check
        user_agent: The user agent string to use for checking
        
    Returns:
        True if the URL's domain is in the allowlist, False otherwise
    """
    # Using urlparse to correctly extract hostname regardless of scheme or port
    parsed_url = urlparse(url)
    return parsed_url.hostname in ALLOWLIST


def is_allowed_by_policy(url: str) -> bool:
    """
    Check if a URL passes our policy checks.
    
    Args:
        url: The URL to check
        
    Returns:
        True if the URL passes policy checks, False otherwise
    """
    return robots_allows(url)