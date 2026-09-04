import json
import pathlib
from typing import Dict

class PageBudget:
    def __init__(self, root: pathlib.Path, max_pages_by_host: Dict[str, int]):
        self.root = root
        self.max_pages_by_host = max_pages_by_host
        self._counters = {}  # host -> count

    def allow(self, host: str) -> bool:
        # Host not in the allowlist is never allowed
        if host not in self.max_pages_by_host:
            return False
            
        # Increment counter for this host
        self._counters[host] = self._counters.get(host, 0) + 1
        count = self._counters[host]
        max_pages = self.max_pages_by_host[host]
        
        # Check if we've exceeded the limit
        if count > max_pages:
            # Write marker file if this is the first time exceeding
            if not self.stopped(host):
                marker_path = self.root / f"STOP-{host}"
                marker_path.write_text(json.dumps({
                    "host": host,
                    "max_pages": max_pages,
                    "reason": "max_pages reached"
                }) + "\n")
            return False
        
        return True

    def stopped(self, host: str) -> bool:
        """Returns whether the marker file exists for this host."""
        marker_path = self.root / f"STOP-{host}"
        return marker_path.exists()