"""P5.3 + P5.4: the frozen synthetic question set, its loader, and recall@k over an injected ranker."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # scripts/ is not a package; make `scripts.eval` importable

from openagentsearch.eval.dataset import load_questions  # noqa: E402
from scripts.eval import recall_at_k, run_eval  # noqa: E402

SYNTHETIC_NAMES = ["synthetic-doc-python", "synthetic-doc-robots", "synthetic-doc-chunking", "synthetic-doc-vectors", "synthetic-doc-api"]
H = [hashlib.sha256(name.encode("utf-8")).hexdigest() for name in SYNTHETIC_NAMES]  # five valid 64-hex ids
X1, X2 = "1" * 64, "2" * 64  # valid syntax, never relevant


def _write(tmpdir: str, name: str, lines: list[str]) -> Path:
    path = Path(tmpdir) / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _row(row_id: str, question: str, hashes: list[str]) -> str:
    return json.dumps({"id": row_id, "question": question, "relevant_doc_sha256": hashes})


def test_committed_question_set_is_frozen_synthetic_and_valid():
    rows = load_questions(REPO / "eval" / "questions.jsonl")
    assert len(rows) == 5
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == 5 and all(i.strip() for i in ids)
    for r in rows:
        assert set(r) == {"id", "question", "relevant_doc_sha256"}
        assert r["question"].strip()
        assert r["relevant_doc_sha256"]
        for h in r["relevant_doc_sha256"]:
            assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
            assert h in H, "committed hash is not one of the five synthetic document identities"
    assert any(len(r["relevant_doc_sha256"]) >= 2 for r in rows), "no multi-relevant row"


def test_load_questions_preserves_order_and_verbatim_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write(tmpdir, "q.jsonl", [
            _row("b", "  second   question with   spaces ", [H[1]]),
            "",
            _row("a", "first? ¿sí? 日本語", [H[0], H[2]]),
            "   ",
            _row("c", "third", [H[3]]),
        ])
        rows = load_questions(path)
        assert [r["id"] for r in rows] == ["b", "a", "c"]
        assert rows[0]["question"] == "  second   question with   spaces "
        assert rows[1]["question"] == "first? ¿sí? 日本語"
        assert rows[1]["relevant_doc_sha256"] == [H[0], H[2]]


def test_malformed_datasets_raise_value_error_naming_the_line():
    good = _row("ok", "fine", [H[0]])
    cases = [
        ([good, '{"id": "x", "question": "unclosed'], 2),                                   # malformed JSON
        ([good, '["not", "an", "object"]'], 2),                                              # non-object line
        ([good, json.dumps({"id": "x", "question": "q"})], 2),                               # missing key
        ([good, json.dumps({"id": "x", "question": "q", "relevant_doc_sha256": [H[0]], "extra": 1})], 2),  # extra key
        ([good, _row("", "q", [H[0]])], 2),                                                  # empty id
        ([good, _row("x", "   ", [H[0]])], 2),                                               # whitespace question
        ([good, _row("x", "q", [])], 2),                                                     # empty relevance list
        ([good, _row("x", "q", ["z" * 64])], 2),                                             # non-hex characters
        ([good, _row("x", "q", [H[0].upper()])], 2),                                         # uppercase hex
        ([good, _row("x", "q", [H[0][:63]])], 2),                                            # 63 characters
        ([good, _row("ok", "q", [H[1]])], 2),                                                # duplicate id
        ([good, _row("x", "q", [H[0], H[0]])], 2),                                           # duplicate hash in a row
        (['{"id": "x", "question": "q", "relevant_doc_sha256": [1]}'], 1),                   # non-string hash, line 1
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        for index, (lines, expected_line) in enumerate(cases):
            path = _write(tmpdir, f"bad{index}.jsonl", lines)
            try:
                load_questions(path)
                assert False, f"case {index} should have raised"
            except ValueError as exc:
                assert f"Line {expected_line}:" in str(exc), (index, str(exc))


def test_recall_at_k_exact_values_and_duplicate_ids_do_not_inflate():
    rows = [
        {"id": "full", "question": "q-full", "relevant_doc_sha256": [H[0]]},
        {"id": "partial", "question": "q-partial", "relevant_doc_sha256": [H[1], H[2]]},
        {"id": "missed", "question": "q-missed", "relevant_doc_sha256": [H[3]]},
    ]
    answers = {
        "q-full": [H[0], H[0], H[0]],      # duplicates of the one relevant hash: recall 1.0, not 3.0
        "q-partial": [H[1], X1, H[2]],     # H[2] is at rank 3, outside k=2: recall 0.5 at k=2, 1.0 at k=3
        "q-missed": [X1, X2],              # nothing relevant: 0.0
    }
    calls = []

    def ranker(question: str, k: int) -> list[str]:
        calls.append((question, k))
        return answers[question]

    assert recall_at_k(rows, ranker, 2) == (1.0 + 0.5 + 0.0) / 3
    assert recall_at_k(rows, ranker, 3) == (1.0 + 1.0 + 0.0) / 3
    assert recall_at_k(rows, ranker, 1) == (1.0 + 0.5 + 0.0) / 3
    assert calls[:3] == [("q-full", 2), ("q-partial", 2), ("q-missed", 2)]
    assert recall_at_k([], ranker, 2) == 0.0
    for bad_k in (0, -1):
        try:
            recall_at_k(rows, ranker, bad_k)
            assert False, "k <= 0 must raise"
        except ValueError:
            pass


def test_run_eval_is_deterministic_and_writes_exactly_the_returned_dict():
    with tempfile.TemporaryDirectory() as tmpdir:
        questions = _write(tmpdir, "questions.jsonl", [_row("a", "alpha", [H[0], H[1]]), _row("b", "beta", [H[2]])])

        def ranker(question: str, k: int) -> list[str]:
            return [H[0], X1, H[1]] if question == "alpha" else [X2, H[2]]

        first = run_eval(questions, Path(tmpdir) / "out" / "one" / "results.json", ranker, 2)
        second = run_eval(questions, Path(tmpdir) / "out" / "two" / "results.json", ranker, 2)
        assert first == second
        assert set(first) == {"k", "question_count", "recall_at_k"}
        assert first["k"] == 2 and first["question_count"] == 2
        assert isinstance(first["recall_at_k"], float) and 0.0 <= first["recall_at_k"] <= 1.0
        assert first["recall_at_k"] == (0.5 + 1.0) / 2
        for sub in ("one", "two"):
            written = (Path(tmpdir) / "out" / sub / "results.json").read_text(encoding="utf-8")
            assert json.loads(written) == first
            assert written == json.dumps(first, sort_keys=True, separators=(",", ":"))
