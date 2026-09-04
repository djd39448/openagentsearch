import hashlib
import json
from pathlib import Path
from typing import Dict, Any

from .store import ExtractStore


class DedupingExtractStore(ExtractStore):
    """An ExtractStore that deduplicates extracted documents by text hash.

    This wrapper around ExtractStore ensures that only unique document contents 
    (based on the text content's SHA256 hash) are stored. Duplicate documents
    with identical text will not be written to disk and will return None.
    """

    def put(self, sha256: str, url: str, extracted: Dict[str, Any], extracted_at: float) -> Path | None:
        """
        Store an extracted document, deduplicating by text content.

        Args:
            sha256: The SHA256 hash of the raw HTML content
            url: The URL from which the content was fetched
            extracted: The extracted content dictionary (text, title, lang)
            extracted_at: Timestamp when extraction occurred
            
        Returns:
            Path to the stored JSON file or None if deduplicated 
        """
        # Compute text hash - exactly as specified in the task
        text_hash = hashlib.sha256(extracted["text"].encode("utf-8")).hexdigest()
        
        # Scan existing extracted files for matching text hash
        extracted_dir = self.root / "extracted"
        if extracted_dir.exists():
            for existing_file in extracted_dir.glob("*.json"):
                try:
                    # Read existing file and compute its text hash 
                    with existing_file.open('r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                        existing_text_hash = hashlib.sha256(existing_data["text"].encode("utf-8")).hexdigest()
                        
                    # If we find a matching text hash, do not store this one
                    if existing_text_hash == text_hash:
                        return None
                except (json.JSONDecodeError, KeyError):
                    # If there's an issue reading/analyzing the file, continue to next
                    continue
        
        # No duplicate found, delegate to parent class
        return super().put(sha256, url, extracted, extracted_at)