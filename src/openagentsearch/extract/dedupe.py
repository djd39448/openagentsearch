import hashlib
import json
from pathlib import Path
from typing import Dict, Any, Optional

from .store import ExtractStore


class DedupingExtractStore:
    """A wrapper around ExtractStore that deduplicates extracted documents by text hash."""
    
    def __init__(self, root: Path) -> None:
        """
        Initialize the DedupingExtractStore with a root directory.
        
        Args:
            root: The root path where extracted documents and raw content are stored
        """
        self.root = root
        self._extract_store = ExtractStore(root)
    
    def put(self, sha256: str, url: str, extracted: Dict[str, Any], extracted_at: float) -> Optional[Path]:
        """
        Store an extracted document, but only if it doesn't already exist by text content.
        
        Args:
            sha256: The SHA256 hash of the raw HTML content
            url: The URL from which the content was fetched
            extracted: The extracted content dictionary (text, title, lang)
            extracted_at: Timestamp when extraction occurred
            
        Returns:
            Path to the stored JSON file, or None if deduplicated
        """
        # Compute the text hash for deduplication
        text_hash = hashlib.sha256(extracted["text"].encode("utf-8")).hexdigest()
        
        # Scan existing extracted files to check for duplicates
        extracted_dir = self.root / "extracted"
        if extracted_dir.exists():
            for file_path in extracted_dir.iterdir():
                if file_path.is_file() and file_path.suffix == ".json":
                    try:
                        with file_path.open("r", encoding="utf-8") as f:
                            existing_data = json.load(f)
                        existing_text_hash = hashlib.sha256(existing_data["text"].encode("utf-8")).hexdigest()
                        if existing_text_hash == text_hash:
                            # Duplicate found, don't store
                            return None
                    except (json.JSONDecodeError, KeyError):
                        # If we can't read or parse the file, continue to next
                        continue
        
        # No duplicate found, proceed with storing
        return self._extract_store.put(sha256, url, extracted, extracted_at)