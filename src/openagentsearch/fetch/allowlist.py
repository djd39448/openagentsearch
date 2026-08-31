from dataclasses import dataclass
from typing import List
import yaml


@dataclass
class AllowlistEntry:
    host: str
    max_pages: int


def load_allowlist(path: str) -> List[AllowlistEntry]:
    """
    Load allowlist from YAML file.
    
    Args:
        path: Path to YAML file containing allowlist entries
        
    Returns:
        List of AllowlistEntry objects
        
    Raises:
        ValueError: If required fields are missing or invalid types
    """
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    
    if not isinstance(data, dict):
        raise ValueError("Allowlist YAML must be a dictionary mapping hostnames to configuration")
    
    entries = []
    for host, config in data.items():
        if not isinstance(host, str):
            raise ValueError(f"Host must be a string, got {type(host)}")
        
        if not isinstance(config, dict):
            raise ValueError(f"Config for {host} must be a dictionary")
            
        max_pages = config.get('max_pages')
        if max_pages is None:
            raise ValueError(f"'max_pages' field missing for host '{host}'")
        
        if not isinstance(max_pages, int):
            raise ValueError(f"'max_pages' for {host} must be an integer, got {type(max_pages)}")
        
        entries.append(AllowlistEntry(host=host, max_pages=max_pages))
    
    return entries