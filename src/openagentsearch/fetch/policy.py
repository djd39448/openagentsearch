"""Policy checking for the fetcher module."""

from urllib.parse import urlparse
from typing import List, Optional
from .allowlist import AllowlistEntry, load_allowlist
from .robots import RobotsPolicy


# Allowlist of domains that are permitted to be fetched
# Default to hardcoded list for backward compatibility
DEFAULT_ALLOWLIST: List[str] = ["docs.python.org"]


def robots_allows(
    url: str, 
    user_agent: str = "OpenAgentSearch-crawler/1.0",
    allowlist: Optional[List[AllowlistEntry]] = None,
    robots_txt: Optional[str] = None
) -> bool:
    """
    Check if the given URL is allowed by robots.txt policy.
    
    First enforces the existing host allowlist exactly as today.
    Then, when robots_txt is provided, evaluates URL through RobotsPolicy.
    
    Args:
        url: The URL to check
        user_agent: The user agent string to use for checking (default: "OpenAgentSearch-crawler/1.0")
        allowlist: Optional list of AllowlistEntry objects. If not provided,
                   uses the hardcoded default DEFAULT_ALLOWLIST.
        robots_txt: Optional robots.txt text to parse and check against
        
    Returns:
        True if the URL passes both allowlist checks and robots checks, False otherwise
    """
    # Using urlparse to correctly extract hostname regardless of scheme or port
    parsed_url = urlparse(url)
    
    # Use provided allowlist or fallback to default
    current_allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
    
    # If it's a list of strings (old way), check directly
    if isinstance(current_allowlist, list) and all(isinstance(item, str) for item in current_allowlist):
        if parsed_url.hostname not in current_allowlist:
            return False
    # If it's a list of AllowlistEntry objects, extract hosts
    elif isinstance(current_allowlist, list) and all(isinstance(item, AllowlistEntry) for item in current_allowlist):
        hosts = [entry.host for entry in current_allowlist]
        if parsed_url.hostname not in hosts:
            return False
    # Fallback for other cases (should not normally happen)
    else:
        if parsed_url.hostname not in DEFAULT_ALLOWLIST:
            return False
    
    # If robots_txt is provided, also check robots policy
    if robots_txt is not None:
        robots_policy = RobotsPolicy(robots_txt, user_agent)
        return robots_policy.is_allowed(url)
    
    # When no robots_txt provided, maintain legacy behavior (allowlist only)
    return True


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