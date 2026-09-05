"""Reopenable on-disk vector store backed by the standard-library sqlite3 module."""

import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Union


class StoreCorruptionError(ValueError):
    """A persisted row is invalid (malformed vector_json, wrong shape or type, non-finite value,
    or a corrupt dimension column). Raised only for data already in SQLite; caller input errors
    in add() stay plain ValueError. The row is never repaired or rewritten."""


class VectorStore:
    """Persist (chunk_id, doc_sha256, vector, text) records to a single SQLite file.

    One connection is held per instance and released by close(); a fresh instance opened on
    the same path with the same dimension reads everything an earlier instance wrote. The
    connection may be used from any thread (the HTTP server answers requests on worker
    threads); every operation is serialized through a lock.
    """

    def __init__(self, path: Union[str, Path], dimension: int) -> None:
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise ValueError("dimension must be a positive integer")

        self.path = Path(path)
        self.dimension = dimension
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self._conn:
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
        with self._lock:
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
        """Physical row count (pure SQL). Rows are not deserialized or validated here, so a
        corrupt row still counts; get() and load_all() are where corruption surfaces."""
        with self._lock:
            row = self._connection().execute("SELECT COUNT(*) FROM vectors").fetchone()
        return int(row[0])

    def _record(self, row: tuple) -> Dict[str, object]:
        chunk_id, doc_sha256, vector_json, text, stored_dimension = row
        if isinstance(stored_dimension, bool) or not isinstance(stored_dimension, int) or stored_dimension <= 0:
            raise StoreCorruptionError(
                f"corrupt dimension column for chunk '{chunk_id}': {stored_dimension!r}"
            )
        if stored_dimension != self.dimension:
            raise StoreCorruptionError(
                f"dimension mismatch for record {chunk_id}: configured {self.dimension}, "
                f"stored {stored_dimension}"
            )
        try:
            parsed = json.loads(vector_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StoreCorruptionError(f"corrupt vector_json for chunk '{chunk_id}'") from exc
        if not isinstance(parsed, list):
            raise StoreCorruptionError(f"corrupt vector_json for chunk '{chunk_id}': not a JSON list")
        if len(parsed) != stored_dimension:
            raise StoreCorruptionError(
                f"corrupt vector_json for chunk '{chunk_id}': {len(parsed)} elements, "
                f"stored dimension {stored_dimension}"
            )
        vector: List[float] = []
        for i, value in enumerate(parsed):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StoreCorruptionError(
                    f"corrupt vector_json for chunk '{chunk_id}': element {i} is not numeric: {value!r}"
                )
            number = float(value)
            if not math.isfinite(number):
                raise StoreCorruptionError(
                    f"corrupt vector_json for chunk '{chunk_id}': element {i} is not finite: {value!r}"
                )
            vector.append(number)
        return {
            "chunk_id": chunk_id,
            "doc_sha256": doc_sha256,
            "vector": vector,
            "text": text,
        }

    def get(self, chunk_id: str) -> Optional[Dict[str, object]]:
        with self._lock:
            row = self._connection().execute(
                "SELECT chunk_id, doc_sha256, vector_json, text, dimension FROM vectors WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
        if row is None:
            return None
        return self._record(row)

    def load_all(self) -> List[Dict[str, object]]:
        with self._lock:
            rows = self._connection().execute(
                "SELECT chunk_id, doc_sha256, vector_json, text, dimension FROM vectors ORDER BY chunk_id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
