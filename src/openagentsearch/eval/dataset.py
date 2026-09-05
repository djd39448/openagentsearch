"""Loader for the frozen evaluation question set (eval/questions.jsonl).

Row schema, exactly: {"id": str, "question": str, "relevant_doc_sha256": list[str]}.
Every validation error names the 1-based line of the offending row.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List

REQUIRED_KEYS = frozenset({"id", "question", "relevant_doc_sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def load_questions(path: str | Path) -> List[Dict[str, Any]]:
    """Read UTF-8 JSONL in file order, validate each non-blank line, return rows verbatim.

    Raises:
        ValueError: for malformed JSON, a non-object line, a key set other than the three schema
            keys, an empty id or question, an empty relevance list, a hash that is not exactly 64
            lowercase ASCII hex characters, a duplicate hash within a row, or a duplicate id across
            rows. The message always starts with "Line <n>:".
    """
    questions: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    with open(Path(path), "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Line {line_no}: invalid JSON - {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Line {line_no}: expected a JSON object, got {type(data).__name__}")
            keys = set(data)
            if keys != REQUIRED_KEYS:
                raise ValueError(
                    f"Line {line_no}: expected exactly the keys {sorted(REQUIRED_KEYS)}, got {sorted(keys)}"
                )
            row_id = data["id"]
            if not isinstance(row_id, str) or not row_id.strip():
                raise ValueError(f"Line {line_no}: id must be a non-empty string")
            if row_id in seen_ids:
                raise ValueError(f"Line {line_no}: duplicate id {row_id!r} (first seen on line {seen_ids[row_id]})")
            question = data["question"]
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"Line {line_no}: question must be a non-empty string")
            hashes = data["relevant_doc_sha256"]
            if not isinstance(hashes, list) or not hashes:
                raise ValueError(f"Line {line_no}: relevant_doc_sha256 must be a non-empty list")
            for position, value in enumerate(hashes):
                if not isinstance(value, str) or not _SHA256.fullmatch(value):
                    raise ValueError(
                        f"Line {line_no}: relevant_doc_sha256[{position}] must be exactly 64 lowercase "
                        f"ASCII hex characters, got {value!r}"
                    )
            if len(set(hashes)) != len(hashes):
                raise ValueError(f"Line {line_no}: duplicate hash within relevant_doc_sha256")
            seen_ids[row_id] = line_no
            questions.append(data)
    return questions
