#!/usr/bin/env python3

import sys
sys.path.insert(0, '/c/Users/trustcore-rdp/openagentsearch/src')

from openagentsearch.eval.dataset import load_questions

def test_basic_functionality():
    print("Testing basic functionality...")
    
    # This uses the actual committed file
    try:
        questions = load_questions('/c/Users/trustcore-rdp/openagentsearch/eval/questions.jsonl')
        print(f"✓ Successfully loaded {len(questions)} questions")
        
        for q in questions:
            print(f"  - ID: {q['id']}")
            print(f"    Question: {q['question']}")
            print(f"    Hashes count: {len(q['relevant_doc_sha256'])}")
            
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        return False

if __name__ == "__main__":
    success = test_basic_functionality()
    sys.exit(0 if success else 1)