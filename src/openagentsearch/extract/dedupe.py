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
        
        # Scan existing extracted files for matching text hash. A malformed or invalid existing
        # record is corpus corruption: fail loudly and do not emit the new document, because a
        # silently skipped record could let a duplicate through.
        extracted_dir = self.root / "extracted"
        if extracted_dir.exists():
            for existing_file in extracted_dir.glob("*.json"):
                try:
                    with existing_file.open('r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed extracted record: {existing_file.name}") from exc
                if not isinstance(existing_data, dict):
                    raise ValueError(
                        f"Malformed extracted record: {existing_file.name} (top level is not a JSON object)"
                    )
                try:
                    existing_text = existing_data["text"]
                except KeyError as exc:
                    raise ValueError(
                        f"Malformed extracted record: {existing_file.name} (missing 'text')"
                    ) from exc
                if not isinstance(existing_text, str):
                    raise ValueError(
                        f"Malformed extracted record: {existing_file.name} ('text' is not a string)"
                    )
                existing_text_hash = hashlib.sha256(existing_text.encode("utf-8")).hexdigest()

                # If we find a matching text hash, do not store this one
                if existing_text_hash == text_hash:
                    return None
        
        # No duplicate found, delegate to parent class
        return super().put(sha256, url, extracted, extracted_at)