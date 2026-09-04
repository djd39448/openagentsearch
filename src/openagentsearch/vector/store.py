"""Reopenable on-disk vector store backed by the standard-library sqlite3 module."""

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Union


class VectorStore:
    """Persist (chunk_id, doc_sha256, vector, text) records to a single SQLite file.

    One connection is held per instance and released by close(); a fresh instance opened on
    the same path with the same dimension reads everything an earlier instance wrote.
    """

    def __init__(self, path: Union[str, Path], dimension: int) -> None:
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise ValueError("dimension must be a positive integer")

        self.path = Path(path)
        self.dimension = dimension
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(self.path)
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    chunk_id TEXT PRIMARY KEY,
                    doc_sha256 TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    dimension INTEGER NOT NULL
                )
                """
            )

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(f"VectorStore at {self.path} is closed")
        return self._conn

    def add(self, chunk_id: str, doc_sha256: str, vector: List[float], text: str) -> None:
        if len(vector) != self.dimension:
            raise ValueError(
                f"vector length {len(vector)} does not match the configured dimension {self.dimension}"
            )
        for i, value in enumerate(vector):
            if isinstance(value, bool):
                raise ValueError(f"vector element at index {i} is a boolean, must be numeric")
            if not isinstance(value, (int, float)):
                raise ValueError(f"vector element at index {i} is not numeric: {value!r}")

        vector_json = json.dumps([float(x) for x in vector], separators=(",", ":"))
        conn = self._connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO vectors (chunk_id, doc_sha256, vector_json, text, dimension) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, doc_sha256, vector_json, text, self.dimension),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"chunk_id '{chunk_id}' already exists") from exc

    def count(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) FROM vectors").fetchone()
        return int(row[0])

    def _record(self, row: tuple) -> Dict[str, object]:
        chunk_id, doc_sha256, vector_json, text, stored_dimension = row
        if stored_dimension != self.dimension:
            raise ValueError(
                f"dimension mismatch for record {chunk_id}: configured {self.dimension}, "
                f"stored {stored_dimension}"
            )
        return {
            "chunk_id": chunk_id,
            "doc_sha256": doc_sha256,
            "vector": [float(x) for x in json.loads(vector_json)],
            "text": text,
        }

    def get(self, chunk_id: str) -> Optional[Dict[str, object]]:
        row = self._connection().execute(
            "SELECT chunk_id, doc_sha256, vector_json, text, dimension FROM vectors WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return self._record(row)

    def load_all(self) -> List[Dict[str, object]]:
        rows = self._connection().execute(
            "SELECT chunk_id, doc_sha256, vector_json, text, dimension FROM vectors ORDER BY chunk_id"
        ).fetchall()
        return [self._record(row) for row in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
