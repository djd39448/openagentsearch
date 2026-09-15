"""Index manifest: a SQLite table recording the outcome of every document indexing attempt, kept
next to — never inside — the vector rows, and written in the same transaction as those rows when
the caller asks for that (see `VectorStore.add_many(..., manifest=...)`).

Standard-library only. Every function here is a pure function of a caller-supplied
`sqlite3.Connection`: none of them ever opens a connection and none of them ever commits — the
caller controls the transaction (`with conn:`), so the manifest row lands atomically with whatever
else the caller is writing in that same transaction.

What this manifest is NOT and does NOT guarantee:

- It is keyed by content hash (`doc_sha256`) and only ever knows about documents whose FULL bytes
  were actually received. A pre-fetch refusal — not in the allowlist, disallowed or unavailable
  robots.txt, page-budget exhaustion, a transport failure, a non-200 status, or a body that was
  too large to keep — leaves no row here at all, because there is no trustworthy content hash to
  key a row by.
- A `superseded` row's chunk rows are NOT deleted from the vector store when a newer document
  supersedes it at the same `source_url`; cosine search may still return them. The manifest only
  records that a newer `indexed` document now exists for that URL.
- A vector store created before this manifest existed has vector rows with no corresponding
  manifest row. `ensure_manifest_table()` only creates the table (if it is missing); it never
  backfills history for rows written earlier.
- Manifest rows read back from storage are validated as `ManifestEntry` instances on the way out;
  a row that fails validation (for example an unrecognized `status`) is never repaired or
  rewritten — it is reported via `ManifestCorruptionError` and left exactly as it was found.
"""

import math
import re
import sqlite3
from dataclasses import dataclass

STATUSES: tuple[str, ...] = ("indexed", "failed", "superseded", "refused")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestEntry:
    """One row of the index manifest: the outcome of one document-level indexing attempt.

    Validated in `__post_init__`; an instance that exists is guaranteed well-formed. That says
    nothing about a row read back from storage by an older or buggy writer — see
    `ManifestCorruptionError`, which is how that case is reported instead.
    """

    doc_sha256: str
    source_url: str
    status: str
    reason: str
    indexed_at: float
    chunk_count: int
    extracted_sha256: str
    source_kind: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")
        if not isinstance(self.doc_sha256, str) or not _SHA256_RE.match(self.doc_sha256):
            raise ValueError(f"doc_sha256 must be 64 lowercase hex chars, got {self.doc_sha256!r}")
        if not isinstance(self.extracted_sha256, str) or not (
            self.extracted_sha256 == "" or _SHA256_RE.match(self.extracted_sha256)
        ):
            raise ValueError(
                "extracted_sha256 must be '' or 64 lowercase hex chars, "
                f"got {self.extracted_sha256!r}"
            )
        if not isinstance(self.source_url, str) or not self.source_url:
            raise ValueError("source_url must be a non-empty string")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise ValueError("source_kind must be a non-empty string")
        bad_chunk_count = (
            isinstance(self.chunk_count, bool)
            or not isinstance(self.chunk_count, int)
            or self.chunk_count < 0
        )
        if bad_chunk_count:
            raise ValueError(f"chunk_count must be a non-negative int, got {self.chunk_count!r}")
        if isinstance(self.indexed_at, bool) or not isinstance(self.indexed_at, (int, float)):
            raise ValueError(f"indexed_at must be a number, got {self.indexed_at!r}")
        if not math.isfinite(self.indexed_at):
            raise ValueError(f"indexed_at must be finite, got {self.indexed_at!r}")
        object.__setattr__(self, "indexed_at", float(self.indexed_at))
        if not isinstance(self.reason, str):
            raise ValueError(f"reason must be a string, got {self.reason!r}")


class ManifestCorruptionError(ValueError):
    """A row read back from the `manifest` table fails `ManifestEntry` validation (an unrecognized
    status, a malformed hash, or a wrong type). Raised only for data already in SQLite; a caller
    building a `ManifestEntry` directly gets a plain `ValueError` instead. The row is never
    repaired or rewritten."""


@dataclass(frozen=True)
class ManifestCounts:
    """Row counts per status, always present (0 when a status has no rows)."""

    indexed: int
    failed: int
    superseded: int
    refused: int

    def as_dict(self) -> dict[str, int]:
        """JSON-shaped view, keys in STATUSES order. Only for the API edge — pass the dataclass
        itself across module boundaries otherwise."""
        return {status: getattr(self, status) for status in STATUSES}


def ensure_manifest_table(conn: sqlite3.Connection) -> None:
    """Create the `manifest` table and its `source_url` index if they do not already exist.

    Pure DDL: never commits and never starts its own transaction, so the caller decides when (or
    whether, inside a larger `with conn:` block) this becomes durable.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manifest (
            doc_sha256 TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            indexed_at REAL NOT NULL,
            chunk_count INTEGER NOT NULL,
            extracted_sha256 TEXT NOT NULL,
            source_kind TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS manifest_source_url ON manifest(source_url)")


def write_manifest_entry(conn: sqlite3.Connection, entry: ManifestEntry) -> None:
    """Upsert one manifest row by `doc_sha256`, running INSIDE the caller's already-open
    transaction (the caller holds `with conn:`); this function never commits on its own.

    When `entry.status == "indexed"`, every OTHER row at the same `source_url` that is currently
    `indexed` is marked `superseded` (reason `f"superseded by {entry.doc_sha256}"`) in the same
    call, so the manifest never claims two rows are simultaneously the live indexed version of one
    URL. Their vector rows are untouched — see the module docstring.

    Concurrency guard: a non-`indexed` write (`failed`, `refused`, ...) for a `doc_sha256` that is
    currently `indexed` is a no-op on the `status`/`reason`/etc. columns — it can never downgrade
    a row a concurrent winner already committed as `indexed`. This matters because two callers can
    legitimately race to index byte-identical content under different source URLs (VectorStore is
    documented as usable from any thread): the loser's SQLite-level failure must not clobber the
    winner's already-committed success. An `indexed` write always applies.
    """
    conn.execute(
        """
        INSERT INTO manifest
            (doc_sha256, source_url, status, reason, indexed_at, chunk_count, extracted_sha256,
             source_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(doc_sha256) DO UPDATE SET
            source_url = excluded.source_url,
            status = excluded.status,
            reason = excluded.reason,
            indexed_at = excluded.indexed_at,
            chunk_count = excluded.chunk_count,
            extracted_sha256 = excluded.extracted_sha256,
            source_kind = excluded.source_kind
        WHERE excluded.status = 'indexed' OR manifest.status != 'indexed'
        """,
        (
            entry.doc_sha256,
            entry.source_url,
            entry.status,
            entry.reason,
            entry.indexed_at,
            entry.chunk_count,
            entry.extracted_sha256,
            entry.source_kind,
        ),
    )
    if entry.status == "indexed":
        conn.execute(
            """
            UPDATE manifest
            SET status = 'superseded', reason = ?
            WHERE source_url = ? AND status = 'indexed' AND doc_sha256 != ?
            """,
            (f"superseded by {entry.doc_sha256}", entry.source_url, entry.doc_sha256),
        )


def read_manifest_counts(conn: sqlite3.Connection) -> ManifestCounts:
    """One `GROUP BY status` query. Every status in STATUSES is present in the result, 0 by
    default. A status value present in the table that is not one of STATUSES is manifest
    corruption, raised as `ManifestCorruptionError`."""
    counts = {status: 0 for status in STATUSES}
    for status, n in conn.execute("SELECT status, COUNT(*) FROM manifest GROUP BY status"):
        if status not in STATUSES:
            raise ManifestCorruptionError(f"manifest contains unrecognized status {status!r}")
        counts[status] = int(n)
    return ManifestCounts(**counts)


def read_manifest_entry(conn: sqlite3.Connection, doc_sha256: str) -> ManifestEntry | None:
    """Read one row by primary key, validated as a `ManifestEntry`. `None` when no row exists for
    `doc_sha256`. A row that fails `ManifestEntry` validation is corruption: raised as
    `ManifestCorruptionError`, never repaired."""
    row = conn.execute(
        "SELECT doc_sha256, source_url, status, reason, indexed_at, chunk_count, extracted_sha256, "
        "source_kind FROM manifest WHERE doc_sha256 = ?",
        (doc_sha256,),
    ).fetchone()
    if row is None:
        return None
    try:
        return ManifestEntry(*row)
    except ValueError as exc:
        raise ManifestCorruptionError(
            f"manifest row for {doc_sha256!r} failed validation: {exc}"
        ) from exc


def read_manifest_entries(conn: sqlite3.Connection) -> list[ManifestEntry]:
    """All rows, ordered by `(source_url, doc_sha256)`, each validated as a `ManifestEntry`. A
    single corrupt row raises `ManifestCorruptionError` and no entries are returned."""
    rows = conn.execute(
        "SELECT doc_sha256, source_url, status, reason, indexed_at, chunk_count, extracted_sha256, "
        "source_kind FROM manifest ORDER BY source_url, doc_sha256"
    ).fetchall()
    entries: list[ManifestEntry] = []
    for row in rows:
        try:
            entries.append(ManifestEntry(*row))
        except ValueError as exc:
            raise ManifestCorruptionError(
                f"manifest row for {row[0]!r} failed validation: {exc}"
            ) from exc
    return entries
