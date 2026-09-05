import json
from pathlib import Path
from typing import List, Dict, Any, Callable

# Type alias for the Ranker function
Ranker = Callable[[str, int], List[str]]

def recall_at_k(rows: List[Dict[str, Any]], ranker: Ranker, k: int) -> float:
    """
    Calculate recall at k for a list of questions.
    
    Args:
        rows: List of question dictionaries with keys: id, question, relevant_doc_sha256
        ranker: Function that takes a question string and k value, returns list of top-k document IDs
        k: The number of top results to consider
        
    Returns:
        The average recall@k across all questions
    """
    if k <= 0:
        raise ValueError("k must be greater than 0")
        
    if not rows:
        return 0.0
        
    total_recall = 0.0
    
    for row in rows:
        question = row['question']
        relevant_hashes = set(row['relevant_doc_sha256'])
        
        # Get ranked results
        ranked_results = ranker(question, k)
        
        # Consider only first k results
        top_k_results = ranked_results[:k]
        
        # Calculate recall: (# of relevant docs retrieved) / (# of relevant docs)
        retrieved_relevant = len(set(top_k_results) & relevant_hashes)
        recall = retrieved_relevant / len(relevant_hashes) if relevant_hashes else 0.0
        
        total_recall += recall
    
    return total_recall / len(rows)

def run_eval(questions_path: str | Path, results_path: str | Path, ranker: Ranker, k: int) -> Dict[str, Any]:
    """
    Run evaluation on questions and save results.
    
    Args:
        questions_path: Path to the questions JSONL file
        results_path: Path where results JSON will be saved
        ranker: Function that takes a question string and k value, returns list of top-k document IDs
        k: The number of top results to consider
        
    Returns:
        Dictionary with keys: k, question_count, recall_at_k
    """
    from openagentsearch.eval.dataset import load_questions
    
    rows = load_questions(questions_path)
    
    score = recall_at_k(rows, ranker, k)
    
    # Prepare result dict
    result_dict = {
        "k": k,
        "question_count": len(rows),
        "recall_at_k": score
    }
    
    # Write results to file
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(result_dict, f, sort_keys=True, separators=(',', ':'))
    
    return result_dict