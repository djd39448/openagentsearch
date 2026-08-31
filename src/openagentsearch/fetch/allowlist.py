from dataclasses import dataclass
from typing import List
import yaml

@dataclass
class AllowlistEntry:
    host: str
    max_pages: int

def load_allowlist(path: str) -> List[AllowlistEntry]:
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    
    entries = []
    for host, config in data.items():
        if not isinstance(config, dict) or 'max_pages' not in config:
            raise ValueError(f"Invalid allowlist entry for {host}")
        
        max_pages = config['max_pages']
        if not isinstance(max_pages, int):
            raise ValueError(f"max_pages must be an integer for {host}")
            
        entries.append(AllowlistEntry(host=host, max_pages=max_pages))
    
    return entries