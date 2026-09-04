import json
import hashlib
from pathlib import Path
from typing import Dict, Any


class ExtractStore:
    """Store extracted documents with provenance verification."""
    
    def __init__(self, root: Path) -> None:
        """
        Initialize the ExtractStore with a root directory.
        
        Args:
            root: The root path where extracted documents will be stored
        """
        self.root = root
    
    def put(self, sha256: str, url: str, extracted: Dict[str, Any], extracted_at: float) -> Path:
        """
        Store an extracted document with provenance verification.
        
        Args:
            sha256: The SHA256 hash of the raw HTML content
            url: The URL from which the content was fetched
            extracted: The extracted content dictionary (text, title, lang)
            extracted_at: Timestamp when extraction occurred
            
        Returns:
            Path to the stored JSON file
            
        Raises:
            ValueError: If provenance verification fails or if any checks fail
        """
        # Step 1: Verify raw hash matches
        raw_file_path = self.root / "raw" / f"{sha256}.html"
        if not raw_file_path.exists():
            raise ValueError("Raw HTML file does not exist")
        
        # Read the raw content and verify its hash
        raw_content = raw_file_path.read_bytes()
        actual_hash = hashlib.sha256(raw_content).hexdigest()
        if actual_hash != sha256:
            raise ValueError("Raw hash mismatch")
        
        # Step 2: Find provenance line with matching SHA256
        provenance_file = self.root / "raw" / "provenance.jsonl"
        provenance_line = None
        if not provenance_file.exists():
            raise ValueError("No provenance")
            
        with provenance_file.open('r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("sha256") == sha256:
                        provenance_line = entry
                        break
                except json.JSONDecodeError:
                    continue  # Skip malformed lines
        
        if not provenance_line:
            raise ValueError("No provenance")
            
        # Step 3: Verify URL matches
        if provenance_line.get("url") != url:
            raise ValueError("URL mismatch")
        
        # Step 4: Write the extracted document
        # Create target directory if needed
        target_dir = self.root / "extracted"
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Create the JSON structure with required fields
        data = {
            "url": url,
            "extracted_at": extracted_at,
            "raw_sha256": sha256,
            "text": extracted["text"],
            "title": extracted["title"],
            "lang": extracted["lang"]
        }
        
        # Write to target file
        output_file = target_dir / f"{sha256}.json"
        output_file.write_text(json.dumps(data, sort_keys=True, ensure_ascii=False))
        
        return output_file