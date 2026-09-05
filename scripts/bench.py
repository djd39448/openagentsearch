"""
Reproducible offline search benchmark for OpenAgentSearch.
This module provides functionality to benchmark search performance with deterministic results.
"""

import json
from pathlib import Path
from typing import Callable, List, Dict, Any
from time import perf_counter

# Import required functions directly from the right modules
from openagentsearch.eval.dataset import load_questions
from openagentsearch.vector.search import cosine_search


def run_benchmark(
    questions_path: str | Path,
    results_path: str | Path,
    store,
    embedder: Callable[[str], List[float]],
    k: int
) -> Dict[str, Any]:
    """
    Run a reproducible offline search benchmark.
    
    Args:
        questions_path: Path to the questions JSONL file
        results_path: Path where results should be written
        store: VectorStore instance to use for searching
        embedder: Function that converts text to embeddings (list of floats)
        k: Number of results to retrieve per question
        
    Returns:
        Dictionary with benchmark results including recall and latency statistics
        
    Raises:
        ValueError: If k <= 0
    """
    if k <= 0:
        raise ValueError("k must be greater than 0")
    
    # Load questions using the official function from eval package
    rows = load_questions(questions_path)
    
    # Measure timing for each question  
    latencies = []
    
    # Run searches and record timings
    for row in rows:
        question = row['question']
        
        # Get embedding
        query_vector = embedder(question)
        
        # Time the search operation only (only the cosine_search call)
        start_time = perf_counter()
        results = cosine_search(store, query_vector, k)
        end_time = perf_counter()
        
        latencies.append(end_time - start_time)
        
        # Verify all results are found in store
        for result in results:
            chunk_id = result['chunk_id']
            record = store.get(chunk_id)
            if not record:
                raise ValueError(f"Chunk {chunk_id} not found in store")
    
    # Compute recall at k using a simple implementation inline to avoid imports
    def calculate_recall_at_k(rows, ranker_func, k):
        """Simple inline implementation of recall_at_k."""
        if k <= 0:
            raise ValueError("k must be greater than 0")
            
        if not rows:
            return 0.0
            
        total_recall = 0.0
        
        for row in rows:
            question = row['question']
            relevant_hashes = set(row['relevant_doc_sha256'])
            
            # Get ranked results using the provided ranker function
            ranked_results = ranker_func(question, k)
            
            # Count how many of the relevant documents are in the top-k results
            retrieved_hashes = set()
            for chunk_id in ranked_results:
                record = store.get(chunk_id)
                if record:
                    # Extract the doc_sha256 from the retrieved record
                    doc_sha256 = str(record['doc_sha256'])
                    retrieved_hashes.add(doc_sha256)
            
            # Calculate recall
            if relevant_hashes:
                recall = len(relevant_hashes & retrieved_hashes) / len(relevant_hashes)
                total_recall += recall
            
        return total_recall / len(rows) if rows else 0.0
    
    # Create a simple ranker function that calls our vector search  
    def ranker_func(question: str, rank_k: int) -> List[str]:
        query_vector = embedder(question)
        search_results = cosine_search(store, query_vector, rank_k)
        return [r['chunk_id'] for r in search_results]
    
    score = calculate_recall_at_k(rows, ranker_func, k)
    
    # Compute latency statistics
    count = len(latencies)
    min_seconds = min(latencies) if latencies else 0.0
    max_seconds = max(latencies) if latencies else 0.0
    mean_seconds = sum(latencies) / count if count > 0 else 0.0
    
    # Prepare results
    result_dict = {
        "k": k,
        "question_count": len(rows),
        "recall_at_k": score,
        "latency": {
            "count": count,
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "mean_seconds": mean_seconds
        }
    }
    
    # Write results to file (parent directory may need to be created)
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(result_dict, f, sort_keys=True, separators=(',', ':'))
    
    return result_dict