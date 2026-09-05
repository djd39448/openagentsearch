import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


def make_doc_route(root: Path) -> Callable[[str, dict[str, list[str]]], tuple[int, dict[str, object]]]:
    """Create a prefix route for fetching extracted documents by SHA256."""
    
    def route(remainder: str, query_dict: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
        # Validate the remainder is exactly 64 lowercase ASCII hex characters
        if not re.fullmatch(r"[0-9a-f]{64}", remainder):
            return (400, {"error": "invalid_sha256"})
        
        doc_sha256 = remainder
        
        # Read the extracted document 
        extracted_path = root / "extracted" / f"{doc_sha256}.json"
        try:
            with open(extracted_path, "r", encoding="utf-8") as f:
                extracted = json.load(f)
        except FileNotFoundError:
            return (404, {"error": "not_found"})
        except (OSError, UnicodeError) as exc:
            raise ValueError("Error reading extracted document") from exc
        except json.JSONDecodeError as e:
            # Malformed JSON in extracted document - raise ValueError as specified
            raise ValueError(f"Malformed extracted JSON: {e}")
        
        # Validate required fields
        required_fields = ["url", "title", "lang", "text", "extracted_at"]
        for field in required_fields:
            if field not in extracted:
                raise ValueError(f"Missing required field '{field}' in extracted document")
        
        # Find provenance info
        provenance = None
        provenance_path = root / "raw" / "provenance.jsonl"
        if provenance_path.exists():
            try:
                with open(provenance_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            prov_entry = json.loads(line)
                        except json.JSONDecodeError:
                            # Skip malformed UNRELATED lines - they're just skipped
                            continue
                        
                        # Check if this entry matches our doc
                        if prov_entry.get("sha256") == doc_sha256 and prov_entry.get("url") == extracted["url"]:
                            # Validate that all required fields exist in the matching entry
                            required_provenance_fields = ["url", "fetched_at", "status", "sha256", "robots_allowed"]
                            for field in required_provenance_fields:
                                if field not in prov_entry:
                                    raise ValueError("Malformed matching provenance entry") from KeyError(f"Missing field '{field}'")
                                
                            provenance = {
                                "url": prov_entry["url"],
                                "fetched_at": prov_entry["fetched_at"],
                                "status": prov_entry["status"],
                                "sha256": prov_entry["sha256"],
                                "robots_allowed": prov_entry["robots_allowed"]
                            }
                            break
            except (OSError, UnicodeError) as exc:
                raise ValueError("Error reading provenance") from exc
        elif not provenance_path.exists():
            # Missing provenance file is valid - treat it as null provenance  
            pass
        
        # Return the document data with appropriate fields
        response_data = {
            "doc_sha256": doc_sha256,
            "url": extracted["url"],
            "title": extracted["title"],
            "lang": extracted["lang"],
            "text": extracted["text"],
            "extracted_at": extracted["extracted_at"],
            "provenance": provenance
        }
        
        return (200, response_data)
    
    return route