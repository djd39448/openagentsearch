"""Policy checking for the fetcher module."""

from urllib.parse import urlparse
from typing import List, Optional
from .allowlist import AllowlistEntry, load_allowlist


# Allowlist of domains that are permitted to be fetched
# Default to hardcoded list for backward compatibility
DEFAULT_ALLOWLIST: List[str] = ["docs.python.org"]


def robots_allows(
    url: str, 
    user_agent: str = "OpenAgentSearch-crawler/1.0",
    allowlist: Optional[List[AllowlistEntry]] = None
) -> bool:
    """
    Check if the given URL is allowed by robots.txt policy.
    
    For now, this is a stub implementation that just checks the allowlist.
    In a full implementation, this would fetch and parse robots.txt.
    
    Args:
        url: The URL to check
        user_agent: The user agent string to use for checking
        allowlist: Optional list of AllowlistEntry objects. If not provided,
                   uses the hardcoded default DEFAULT_ALLOWLIST.
        
    Returns:
        True if the URL's domain is in the allowlist, False otherwise
    """
    # Using urlparse to correctly extract hostname regardless of scheme or port
    parsed_url = urlparse(url)
    
    # Use provided allowlist or fallback to default
    current_allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
    
    # If it's a list of strings (old way), check directly
    if isinstance(current_allowlist, list) and all(isinstance(item, str) for item in current_allowlist):
        return parsed_url.hostname in current_allowlist
    
    # If it's a list of AllowlistEntry objects, extract hosts
    elif isinstance(current_allowlist, list) and all(isinstance(item, AllowlistEntry) for item in current_allowlist):
        hosts = [entry.host for entry in current_allowlist]
        return parsed_url.hostname in hosts
    
    # Fallback for other cases (should not normally happen)
    else:
        return parsed_url.hostname in DEFAULT_ALLOWLIST


def is_allowed_by_policy(url: str, allowlist: Optional[List[AllowlistEntry]] = None) -> bool:
    """
    Check if a URL passes our policy checks.
    
    Args:
        url: The URL to check
        allowlist: Optional list of AllowlistEntry objects. If not provided,
                   uses the hardcoded default DEFAULT_ALLOWLIST.
        
    Returns:
        True if the URL passes policy checks, False otherwise
    """
    return robots_allows(url, allowlist=allowlist)