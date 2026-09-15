"""Reopenable on-disk vector store backed by the standard-library sqlite3 module."""

import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from openagentsearch.index.manifest import (
    ManifestCounts,
    ManifestEntry,
    ensure_manifest_table,
    read_manifest_counts,
    read_manifest_entries,
    read_manifest_entry,
    write_manifest_entry,
)


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
            ensure_manifest_table(self._conn)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(f"VectorStore at {self.path} is closed")
        return self._conn

    def _vector_json(self, vector: Sequence[float]) -> str:
        """Validate one caller-supplied vector and return its canonical JSON. Pure: touches no SQL."""
        if len(vector) != self.dimension:
            raise ValueError(
                f"vector length {len(vector)} does not match the configured dimension {self.dimension}"
            )
        for i, value in enumerate(vector):
            if isinstance(value, bool):
                raise ValueError(f"vector element at index {i} is a boolean, must be numeric")
            if not isinstance(value, (int, float)):
                raise ValueError(f"vector element at index {i} is not numeric: {value!r}")
        return json.dumps([float(x) for x in vector], separators=(",", ":"))

    def add(self, chunk_id: str, doc_sha256: str, vector: List[float], text: str) -> None:
        vector_json = self._vector_json(vector)
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

    def add_many(
        self,
        records: Iterable[Tuple[str, str, Sequence[float], str]],
        *,
        manifest: Optional[ManifestEntry] = None,
    ) -> int:
        """Insert every (chunk_id, doc_sha256, vector, text) record in ONE SQLite transaction, and,
        when `manifest` is given, upsert that manifest row in the SAME transaction — a failed
        insert rolls back the manifest row too.

        All-or-nothing: every record is validated before the first SQL statement runs, and the
        insert (with the manifest upsert, when given) is a single transaction, so a bad vector, a
        chunk_id repeated inside the batch, or a chunk_id that already exists in the store leaves
        the file exactly as it was. Returns the number of rows written. An empty batch with
        `manifest=None` returns 0 and writes nothing, as before; an empty batch WITH a manifest
        still writes the manifest row (in its own transaction) and returns 0.
        """
        rows: List[Tuple[str, str, str, str, int]] = []
        seen: set = set()
        for record in records:
            if not isinstance(record, tuple) or len(record) != 4:
                raise ValueError("each record must be a (chunk_id, doc_sha256, vector, text) tuple")
            chunk_id, doc_sha256, vector, text = record
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("chunk_id must be a non-empty string")
            if not isinstance(doc_sha256, str) or not isinstance(text, str):
                raise ValueError("doc_sha256 and text must be strings")
            if chunk_id in seen:
                raise ValueError(f"chunk_id '{chunk_id}' is repeated within the batch")
            seen.add(chunk_id)
            rows.append((chunk_id, doc_sha256, self._vector_json(vector), text, self.dimension))
        if not rows:
            if manifest is not None:
                with self._lock:
                    conn = self._connection()
                    with conn:
                        write_manifest_entry(conn, manifest)
            return 0
        with self._lock:
            conn = self._connection()
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO vectors (chunk_id, doc_sha256, vector_json, text, dimension) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    if manifest is not None:
                        write_manifest_entry(conn, manifest)
            except sqlite3.IntegrityError as exc:
                existing = self._existing_ids_locked([row[0] for row in rows])
                clash = next((row[0] for row in rows if row[0] in existing), None)
                raise ValueError(f"chunk_id '{clash}' already exists; no rows from the batch were written") from exc
        return len(rows)

    def record_manifest(self, entry: ManifestEntry) -> None:
        """Write one manifest row (typically a `failed` or `refused` outcome) in its own
        transaction, independent of any vector rows. Upserts by `doc_sha256`, same rule as
        `add_many(manifest=...)`."""
        with self._lock:
            conn = self._connection()
            with conn:
                write_manifest_entry(conn, entry)

    def manifest_counts(self) -> ManifestCounts:
        """Counts of manifest rows by status. Raises `ManifestCorruptionError` if the table
        contains a status outside `STATUSES`."""
        with self._lock:
            return read_manifest_counts(self._connection())

    def manifest_entry(self, doc_sha256: str) -> Optional[ManifestEntry]:
        """One manifest row by document hash, or `None` when absent. Raises
        `ManifestCorruptionError` if the stored row fails `ManifestEntry` validation."""
        with self._lock:
            return read_manifest_entry(self._connection(), doc_sha256)

    def manifest_entries(self) -> List[ManifestEntry]:
        """All manifest rows, ordered by `(source_url, doc_sha256)`."""
        with self._lock:
            return read_manifest_entries(self._connection())

    def _existing_ids_locked(self, chunk_ids: Sequence[str]) -> set:
        """Caller holds self._lock. Batched so the query stays under SQLite's bound-parameter limit."""
        conn = self._connection()
        found: set = set()
        step = 500
        for start in range(0, len(chunk_ids), step):
            batch = list(chunk_ids[start : start + step])
            marks = ",".join("?" for _ in batch)
            for (chunk_id,) in conn.execute(f"SELECT chunk_id FROM vectors WHERE chunk_id IN ({marks})", batch):
                found.add(chunk_id)
        return found

    def existing_chunk_ids(self, chunk_ids: Iterable[str]) -> set:
        """Return the subset of `chunk_ids` already present in the store. Read-only; nothing is
        deserialized, so a corrupt row still counts as present."""
        ids = list(chunk_ids)
        for chunk_id in ids:
            if not isinstance(chunk_id, str):
                raise ValueError("chunk ids must be strings")
        if not ids:
            return set()
        with self._lock:
            return self._existing_ids_locked(ids)

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
