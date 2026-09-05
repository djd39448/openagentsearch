import urllib.parse
from collections.abc import Callable
from typing import Protocol

from openagentsearch.vector.store import VectorStore
from openagentsearch.vector.search import cosine_search


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


DocURLResolver = Callable[[str], str | None]


def make_search_route(
    store: VectorStore,
    embedder: Embedder,
    resolve_doc_url: DocURLResolver
) -> Callable[[dict[str, list[str]]], tuple[int, dict[str, object]]]:
    """Create a JSONRoute for search endpoint with injected dependencies."""
    
    def route(query_dict: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
        # Parse query parameters
        q_param = query_dict.get("q")
        if not q_param or not q_param[0].strip():
            return (400, {"error": "missing_query"})
        
        q = q_param[0].strip()
        
        # Parse k parameter
        k_param = query_dict.get("k")
        if not k_param:
            k = 10
        else:
            try:
                k = int(k_param[0])
                if k <= 0:
                    return (400, {"error": "invalid_k"})
            except ValueError:
                return (400, {"error": "invalid_k"})
        
        # Get the query vector from the embedder
        query_vector = embedder.embed(q)
        
        # Search for results
        results_list = cosine_search(store, query_vector, k)
        
        # Build response
        results = []
        for chunk_id, score in results_list:
            record = store.get(chunk_id)
            if record is None:
                continue  # Skip if the record was not found
            
            doc_url = resolve_doc_url(str(record["doc_sha256"]))
            
            result_entry = {
                "chunk_id": chunk_id,
                "doc_sha256": record["doc_sha256"],
                "doc_url": doc_url,
                "score": score,
                "snippet": str(record["text"])[:200],
            }
            results.append(result_entry)
        
        return (200, {"query": q, "k": k, "results": results})
    
    return route