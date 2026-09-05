"""P6.3: the offline benchmark runs the real cosine search over a seeded store and is reproducible."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # scripts/ is not a package

from openagentsearch.eval.dataset import load_questions  # noqa: E402
from openagentsearch.vector.store import VectorStore  # noqa: E402
from scripts.bench import run_benchmark  # noqa: E402

QUESTIONS = REPO / "eval" / "questions.jsonl"
SYNTHETIC = {
    name: hashlib.sha256(name.encode("utf-8")).hexdigest()
    for name in ("synthetic-doc-python", "synthetic-doc-robots", "synthetic-doc-chunking", "synthetic-doc-vectors", "synthetic-doc-api")
}
# One distinct direction per synthetic document, so a question embedded as its first relevant
# document's direction ranks that document first.
SEED_VECTORS = {
    SYNTHETIC["synthetic-doc-python"]: [1.0, 0.0],
    SYNTHETIC["synthetic-doc-robots"]: [0.0, 1.0],
    SYNTHETIC["synthetic-doc-chunking"]: [-1.0, 0.0],
    SYNTHETIC["synthetic-doc-vectors"]: [0.0, -1.0],
    SYNTHETIC["synthetic-doc-api"]: [0.7071, 0.7071],
}


def _seed_store(tmpdir: str, chunks: dict[str, tuple[str, list[float]]]) -> VectorStore:
    """chunks: chunk_id -> (doc_sha256, vector); dimension 2."""
    store = VectorStore(Path(tmpdir) / "vectors.sqlite3", 2)
    for chunk_id, (doc_sha256, vector) in chunks.items():
        store.add(chunk_id, doc_sha256, vector, f"text of {chunk_id}")
    return store


def _committed_fixture(tmpdir: str):
    """Seeded store with every synthetic document + an embedder keyed on the committed questions."""
    rows = load_questions(QUESTIONS)
    chunks = {f"chunk-{i}": (sha, vec) for i, (sha, vec) in enumerate(SEED_VECTORS.items())}
    store = _seed_store(tmpdir, chunks)
    question_vectors = {row["question"]: SEED_VECTORS[row["relevant_doc_sha256"][0]] for row in rows}

    def embedder(question: str) -> list[float]:
        return list(question_vectors[question])

    return rows, store, embedder


def test_committed_set_benchmark_has_exact_schema():
    with tempfile.TemporaryDirectory() as tmpdir:
        rows, store, embedder = _committed_fixture(tmpdir)
        try:
            result = run_benchmark(QUESTIONS, Path(tmpdir) / "out" / "results.json", store, embedder, 3)
        finally:
            store.close()
    assert list(result.keys()) == ["k", "question_count", "recall_at_k", "latency"]
    assert list(result["latency"].keys()) == ["count", "min_seconds", "max_seconds", "mean_seconds"]
    assert result["k"] == 3 and result["question_count"] == len(rows) == 5
    assert isinstance(result["recall_at_k"], float) and 0.0 <= result["recall_at_k"] <= 1.0
    assert result["latency"]["count"] == 5
    for key in ("min_seconds", "max_seconds", "mean_seconds"):
        assert isinstance(result["latency"][key], float) and result["latency"][key] >= 0.0
    assert result["latency"]["min_seconds"] <= result["latency"]["mean_seconds"] <= result["latency"]["max_seconds"]


def test_two_runs_agree_on_everything_but_latency():
    with tempfile.TemporaryDirectory() as tmpdir:
        rows, store, embedder = _committed_fixture(tmpdir)
        first_path = Path(tmpdir) / "one" / "results.json"
        second_path = Path(tmpdir) / "two" / "results.json"
        try:
            first = run_benchmark(QUESTIONS, first_path, store, embedder, 2)
            second = run_benchmark(QUESTIONS, second_path, store, embedder, 2)
        finally:
            store.close()
        for key in ("k", "question_count", "recall_at_k"):
            assert first[key] == second[key], key
        assert first["latency"]["count"] == second["latency"]["count"] == 5
        assert json.loads(first_path.read_text(encoding="utf-8")) == first
        assert json.loads(second_path.read_text(encoding="utf-8")) == second
        assert first_path.read_text(encoding="utf-8") == json.dumps(first, sort_keys=True, separators=(",", ":"))


def test_recall_is_computed_through_the_real_cosine_ranking():
    a, b, c, d, x = "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64  # x is never relevant
    with tempfile.TemporaryDirectory() as tmpdir:
        questions = Path(tmpdir) / "q.jsonl"
        questions.write_text(
            "\n".join([
                json.dumps({"id": "q1", "question": "one", "relevant_doc_sha256": [a]}),
                json.dumps({"id": "q2", "question": "two", "relevant_doc_sha256": [b, c]}),
                json.dumps({"id": "q3", "question": "three", "relevant_doc_sha256": [d]}),
            ]) + "\n",
            encoding="utf-8",
        )
        store = _seed_store(tmpdir, {
            "a-chunk": (a, [1.0, 0.0]),
            "b-chunk": (b, [0.0, 1.0]),
            "c-chunk": (c, [-1.0, 0.0]),
            "d-chunk": (d, [0.0, -1.0]),
            "x-chunk": (x, [0.9, 0.1]),  # non-relevant document sitting right next to "one"
        })
        vectors = {"one": [1.0, 0.0], "two": [0.0, 1.0], "three": [0.9, 0.1]}
        embedder = lambda question: list(vectors[question])  # noqa: E731
        try:
            # k=1: q1 -> a (cos 1.0 beats x at 0.994) = 1.0; q2 -> b only = 0.5; q3 -> x, not d = 0.0.
            k1 = run_benchmark(questions, Path(tmpdir) / "k1.json", store, embedder, 1)
            # k=4: q2's ranking is b, x, a, c (c enters at rank 4) = 1.0; q3's is x, a, b, d = 1.0.
            k4 = run_benchmark(questions, Path(tmpdir) / "k4.json", store, embedder, 4)
        finally:
            store.close()
    assert k1["recall_at_k"] == (1.0 + 0.5 + 0.0) / 3
    assert k4["recall_at_k"] == 1.0
    assert k1["question_count"] == 3 and k1["latency"]["count"] == 3


def test_invalid_k_raises_before_any_file_is_written():
    with tempfile.TemporaryDirectory() as tmpdir:
        rows, store, embedder = _committed_fixture(tmpdir)
        target = Path(tmpdir) / "never" / "results.json"
        try:
            for bad_k in (0, -1, True, 2.0):
                try:
                    run_benchmark(QUESTIONS, target, store, embedder, bad_k)
                    assert False, f"k={bad_k!r} must raise"
                except ValueError:
                    pass
        finally:
            store.close()
        assert not target.exists() and not target.parent.exists()
