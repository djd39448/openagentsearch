"""Reproducible offline benchmark of the local search stack (ROADMAP P6.3).

run_benchmark() ranks each frozen question through the REAL cosine search over a VectorStore,
using an injected embedder, measures only the search call, and records recall@k (from
scripts/eval.py) plus latency statistics. Everything except the latency numbers is
deterministic for identical store / questions / embedder inputs.
"""

import json
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ is not a package

from openagentsearch.eval.dataset import load_questions  # noqa: E402
from openagentsearch.vector.search import cosine_search  # noqa: E402
from openagentsearch.vector.store import VectorStore  # noqa: E402
from scripts.eval import recall_at_k  # noqa: E402

Embedder = Callable[[str], List[float]]


def run_benchmark(
    questions_path: str | Path,
    results_path: str | Path,
    store: VectorStore,
    embedder: Embedder,
    k: int,
) -> Dict[str, object]:
    """Benchmark the search stack over the frozen question set and write results_path.

    Raises:
        ValueError: if k is not a positive integer (checked before anything is written), or if a
            ranked chunk_id has no record in the store (the ranking is never silently shortened).
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")

    rows = load_questions(questions_path)
    latencies: List[float] = []

    def ranker(question: str, top_k: int) -> List[str]:
        query_vector = embedder(question)
        started = time.perf_counter()
        ranked = cosine_search(store, query_vector, top_k)
        latencies.append(time.perf_counter() - started)
        doc_hashes: List[str] = []
        for chunk_id, _score in ranked:
            record = store.get(chunk_id)
            if record is None:
                raise ValueError(f"ranked chunk {chunk_id!r} has no record in the store")
            doc_hashes.append(str(record["doc_sha256"]))
        return doc_hashes

    recall = recall_at_k(rows, ranker, k)

    count = len(latencies)
    latency = {
        "count": count,
        "min_seconds": min(latencies) if count else 0.0,
        "max_seconds": max(latencies) if count else 0.0,
        "mean_seconds": (sum(latencies) / count) if count else 0.0,
    }
    result: Dict[str, object] = {
        "k": k,
        "question_count": len(rows),
        "recall_at_k": recall,
        "latency": latency,
    }

    target = Path(results_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return result
