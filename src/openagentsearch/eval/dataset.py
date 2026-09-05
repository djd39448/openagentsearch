import hashlib
import json
from pathlib import Path
from typing import List, Dict, Any

def load_questions(path: str | Path) -> List[Dict[str, Any]]:
    """
    Load questions from a JSONL file.
    
    Args:
        path: Path to the JSONL file
        
    Returns:
        List of question dictionaries with keys: id, question, relevant_doc_sha256
        
    Raises:
        ValueError: If any row is malformed or doesn't meet schema requirements
    """
    path = Path(path)
    questions = []
    
    with open(path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:  # Skip blank lines
                continue
                
            try:
                data = json.loads(line)  # Use json.loads instead of eval
            except Exception as e:
                raise ValueError(f"Line {i}: Invalid JSON - {str(e)}")
            
            # Validate schema
            required_keys = {'id', 'question', 'relevant_doc_sha256'}
            if not all(key in data for key in required_keys):
                raise ValueError(f"Line {i}: Missing required keys. Got {set(data.keys())}, expected {required_keys}")
                
            # Validate id
            if not isinstance(data['id'], str) or not data['id'].strip():
                raise ValueError(f"Line {i}: id must be a non-empty string")
                
            # Validate question
            if not isinstance(data['question'], str) or not data['question'].strip():
                raise ValueError(f"Line {i}: question must be a non-empty string")
                
            # Validate relevant_doc_sha256
            if not isinstance(data['relevant_doc_sha256'], list):
                raise ValueError(f"Line {i}: relevant_doc_sha256 must be a list")
                
            if len(data['relevant_doc_sha256']) == 0:
                raise ValueError(f"Line {i}: relevant_doc_sha256 must not be empty")
                
            # Validate each hash
            for j, hash_val in enumerate(data['relevant_doc_sha256']):
                if not isinstance(hash_val, str):
                    raise ValueError(f"Line {i}: Hash at position {j} must be a string")
                    
                if len(hash_val) != 64:
                    raise ValueError(f"Line {i}: Hash at position {j} must be exactly 64 characters long")
                    
                if not all(c in '0123456789abcdef' for c in hash_val):
                    raise ValueError(f"Line {i}: Hash at position {j} must contain only lowercase hexadecimal characters")
            
            questions.append(data)
    
    # Validate unique IDs
    ids = [q['id'] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("All question IDs must be unique")
        
    return questions