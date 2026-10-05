"""The Worker-sized reputation-ledger artifact: `did-ledger-compact.json` (package B2).

The full ledger (`ledger.to_jsonl_bytes`) ran 43.7 MB on the live 2026-09-16 log -- too heavy to
bundle whole into the Cloudflare Worker next to the 4.7 MB lexical index (`handoff/B2-SPEC.md`
"Why"). This module builds a smaller artifact instead: the full row (`facts` + `score`, exactly
the JSONL shape -- see `ledger.facts_to_obj`/`ledger.score_to_obj`) for every DID that is NOT a
burst member, and a four-field summary (`burst_id`, `first_seen_ts`, `post_count`,
`max_posts_per_minute`) for every DID that IS one. Burst members made up the overwhelming
majority of DIDs on the live log (49,558 of 53,856 measured 2026-09-16) but a burst member always
scores exactly `0.0` (`score.Score`'s own invariant), so their full evidence trail is not worth
shipping to every Worker request.

The artifact is size-bounded by construction (package CT, 2026-09-30): once the whole ledger no
longer fits `max_bytes`, `fit_compact_json_bytes()` keeps the most recently active DIDs -- every DID
whose `last_seen_ts` is at or after a cutoff, none before it -- and records that cutoff in an
optional `served` header (`{"dids": M, "last_seen_min_ts": T}`); `dids` stays the WHOLE ledger's
count. The full ledger (sharded as `did-ledger/` on GitHub Pages, see reputation.shards) still
carries every DID. Measured on the
2026-09-30 log: 32,338 of 44,236 DIDs had posted within 7 days, so a time window alone would not
bound the size -- only a byte budget does.

`to_compact_json_bytes()` / `load_compact_ledger()` are the write/read halves of one contract, the
same convention `ledger.py`'s `to_jsonl_bytes()`/`load_ledger()` and `lexical.index`'s
`to_json_bytes()`/`load_lexical_index()` use: anything the writer produces, the loader reads back
to an equal value, and the loader accepts nothing the writer would not itself produce.
`CompactLedger.lookup()` returns the exact `/did/{did}` response body (`docs/api.md`) for a known
DID -- the SAME shape `openagentsearch.api.did`, the Cloudflare Worker, and both MCP tools answer
(`handoff/B2-SPEC.md` item 2, "one shape everywhere").

NOT guaranteed: exactly the same caveats as `ledger.Ledger` -- a snapshot as of `generated_at`,
no signature verification anywhere upstream of this file, and a score is evidence from one log,
never an endorsement.
"""

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openagentsearch.reputation.ledger import (
    Ledger,
    LedgerRow,
    facts_to_obj,
    obj_to_facts,
    obj_to_score,
    score_to_obj,
)

SCHEMA_COMPACT = "openagentsearch.did-ledger-compact/1"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# The `/did/{did}` response body for a known DID (`docs/api.md`): a plain JSON-shaped dict, never
# constructed with a single fixed key set -- a non-burst body and a burst-member body carry
# different keys (`burst_id` only on the latter) -- see `CompactLedger.lookup`.
DidAnswer = dict[str, object]


def _format_number(value: float) -> str:
    """`value` (a JSON number round-tripped through `json.load`, e.g. `BurstRecord.first_seen_ts`)
    formatted exactly the way JavaScript's `Number.prototype.toString()` formats the SAME value on
    the Worker side (`worker/src/routes.js`'s `String(firstSeenTs)`) -- the "one shape everywhere"
    contract (`handoff/B2-SPEC.md` item 2) requires the `facts_used` string to be byte-identical
    across the A2 server and the Worker/MCP tools for the identical stored value.

    Python's `str()`/`repr()` on a `float` always keeps a trailing `.0` for an integral value
    (`str(1757136000.0) == "1757136000.0"`); JS's `String()` never does
    (`String(1757136000) === "1757136000"`). Every whole-second Unix timestamp -- the overwhelming
    common case for `first_seen_ts` -- would otherwise diverge. For a non-integral value, Python's
    `repr()` and JS's `String()` already agree (both print the shortest round-tripping decimal).
    """
    return str(int(value)) if value.is_integer() else repr(value)


@dataclass(frozen=True)
class BurstRecord:
    """The four facts kept for a burst-member DID in the compact artifact -- everything
    `CompactLedger.lookup()` needs to answer for one, and nothing else: a burst member's `score`
    is always exactly `0.0` (`score.Score`'s own invariant) and its `facts_used` is fully
    reconstructable from these four fields alone (see `CompactLedger.lookup`), so the rest of its
    `DidFacts` is never carried here."""

    burst_id: int | None
    first_seen_ts: float
    post_count: int
    max_posts_per_minute: int

    def __post_init__(self) -> None:
        if self.burst_id is not None and (
            isinstance(self.burst_id, bool)
            or not isinstance(self.burst_id, int)
            or self.burst_id < 0
        ):
            raise ValueError(f"burst_id must be a non-negative int or None, got {self.burst_id!r}")
        if isinstance(self.first_seen_ts, bool) or not isinstance(self.first_seen_ts, (int, float)):
            raise ValueError(f"first_seen_ts must be a number, got {self.first_seen_ts!r}")
        object.__setattr__(self, "first_seen_ts", float(self.first_seen_ts))
        for name, value in (
            ("post_count", self.post_count),
            ("max_posts_per_minute", self.max_posts_per_minute),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")


@dataclass(frozen=True)
class ServedScope:
    """Present only on a size-fitted artifact (`fit_compact_json_bytes`): it holds `dids` DIDs,
    exactly those whose `last_seen_ts >= last_seen_min_ts`, out of the ledger's full `dids`."""

    dids: int
    last_seen_min_ts: float

    def __post_init__(self) -> None:
        if isinstance(self.dids, bool) or not isinstance(self.dids, int) or self.dids < 1:
            raise ValueError(f"served.dids must be a positive int, got {self.dids!r}")
        ts = self.last_seen_min_ts
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ValueError(f"served.last_seen_min_ts must be a finite number, got {ts!r}")
        object.__setattr__(self, "last_seen_min_ts", float(self.last_seen_min_ts))

    def to_obj(self) -> dict[str, object]:
        return {"dids": self.dids, "last_seen_min_ts": self.last_seen_min_ts}


def to_compact_json_bytes(ledger: Ledger) -> bytes:
    """`ledger` -> the canonical UTF-8 bytes of its compact Worker artifact: one JSON object,
    `ensure_ascii=False`, sorted keys, compact separators -- byte-deterministic, the same
    convention `lexical.index.to_json_bytes` uses (no trailing newline: this file is imported
    directly as JSON by the Worker, `with { type: "json" }`, not read line-by-line).

    Every DID in `ledger.rows` appears in EXACTLY ONE of `non_burst` (keyed by `did`, the full
    `{"facts": ..., "score": ...}` row -- the same shape `ledger.to_jsonl_bytes` writes, minus the
    outer `"did"` key, already the map key) or `burst` (keyed by `did`, `[burst_id, first_seen_ts,
    post_count, max_posts_per_minute]`) -- partitioned by `row.score.burst`
    (`facts.is_burst_member`'s own outcome at build time), NOT by `row.facts.burst_id` alone (a
    DID can be a burst member purely by posting RATE, with `burst_id is None` -- see
    `facts.is_burst_member`).
    """
    return _dumps(_compact_obj(ledger, ledger.rows, None))


def _dumps(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _entry(row: LedgerRow) -> tuple[bool, Any]:
    """`(is_burst, value)`: the value `row` is stored under in `burst` or `non_burst`."""
    if row.score.burst:
        return True, [
            row.facts.burst_id,
            row.facts.first_seen_ts,
            row.facts.post_count,
            row.facts.max_posts_per_minute,
        ]
    return False, {"facts": facts_to_obj(row.facts), "score": score_to_obj(row.score)}


def _compact_obj(
    ledger: Ledger, rows: Sequence[LedgerRow], served: "ServedScope | None"
) -> dict[str, Any]:
    non_burst: dict[str, Any] = {}
    burst: dict[str, Any] = {}
    for row in rows:
        is_burst, value = _entry(row)
        (burst if is_burst else non_burst)[row.facts.did] = value
    obj: dict[str, Any] = {
        "schema": SCHEMA_COMPACT,
        "generated_at": ledger.generated_at,
        "log_rows": ledger.log_rows,
        "posts": ledger.posts,
        "dids": ledger.dids,
        "bursts": ledger.bursts,
        "non_burst": non_burst,
        "burst": burst,
    }
    if served is not None:
        obj["served"] = served.to_obj()
    return obj


class CompactLedgerSizeError(ValueError):
    """Raised by `fit_compact_json_bytes()`/`write_compact_ledger()` when not even the single most
    recently active DID fits in `max_bytes` -- raised BEFORE any file (not even a temp file) is
    created, the same convention `ledger.LedgerSizeError` uses."""


def fit_compact_json_bytes(ledger: Ledger, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """The compact artifact for `ledger`, at most `max_bytes` long (see `_fit`)."""
    return _fit(ledger, max_bytes)[0]


def _fit(ledger: Ledger, max_bytes: int) -> tuple[bytes, "ServedScope | None"]:
    """`(data, served)`: the compact artifact for `ledger`, at most `max_bytes` long, and its
    `served` scope (`None` when every DID fits).

    When the whole ledger fits, this is exactly `to_compact_json_bytes(ledger)` (no `served` key).
    Otherwise the DIDs are ranked by `facts.last_seen_ts`, newest first, and the artifact keeps the
    longest run that fits, cut back to a timestamp boundary so the kept set is EXACTLY "every DID
    last seen at or after `served.last_seen_min_ts`" -- a tie group at the cutoff is kept whole or
    dropped whole. The result is deterministic for a given ledger and budget.

    Raises:
        CompactLedgerSizeError: not even the newest timestamp group fits in `max_bytes`.
    """
    full = to_compact_json_bytes(ledger)
    if len(full) <= max_bytes:
        return full, None
    rows = sorted(ledger.rows, key=lambda r: (-r.facts.last_seen_ts, r.facts.did))
    # Exact size of a prefix without serializing it: `sort_keys` output is the header with both
    # maps empty, plus each entry's `"did":value` bytes, plus one comma between entries of a map.
    entry_bytes: list[int] = []
    is_burst: list[bool] = []
    for row in rows:
        burst_entry, value = _entry(row)
        entry_bytes.append(len(_dumps({row.facts.did: value})) - 2)
        is_burst.append(burst_entry)
    prefix_bytes = [0]
    prefix_burst = [0]
    for size, burst_entry in zip(entry_bytes, is_burst):
        prefix_bytes.append(prefix_bytes[-1] + size)
        prefix_burst.append(prefix_burst[-1] + int(burst_entry))

    def size_of(k: int) -> int:
        served = ServedScope(dids=k, last_seen_min_ts=rows[k - 1].facts.last_seen_ts)
        header = len(_dumps(_compact_obj(ledger, (), served)))
        n_burst = prefix_burst[k]
        n_non_burst = k - n_burst
        return header + prefix_bytes[k] + max(n_burst - 1, 0) + max(n_non_burst - 1, 0)

    lo, hi = 0, len(rows)  # invariant: size_of(lo) fits (lo == 0 means "nothing yet")
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size_of(mid) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    k = lo
    # Cut back to a timestamp boundary: the kept set must be "every DID at or after the cutoff".
    while 0 < k < len(rows) and rows[k].facts.last_seen_ts == rows[k - 1].facts.last_seen_ts:
        k -= 1
    if k == 0:
        raise CompactLedgerSizeError(
            f"compact ledger is {len(full)} bytes and not even the most recently active DID "
            f"fits in the {max_bytes} byte limit"
        )
    served = ServedScope(dids=k, last_seen_min_ts=rows[k - 1].facts.last_seen_ts)
    data = _dumps(_compact_obj(ledger, rows[:k], served))
    if len(data) > max_bytes:  # size_of() is exact; this guards the arithmetic, not the data
        raise AssertionError(f"fitted compact ledger is {len(data)} bytes > {max_bytes}")
    return data, served


def write_compact_ledger(
    ledger: Ledger, out_path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> int:
    """Serialize `ledger`'s compact artifact, fitted to `max_bytes` (`fit_compact_json_bytes`), and
    write it to `out_path` atomically (temp file in the same directory, then `os.replace`),
    returning the byte count written.

    Raises:
        CompactLedgerSizeError: not even one DID fits in `max_bytes`. Checked BEFORE any write,
            so a refusal leaves `out_path`'s directory exactly as it was found.
    """
    return write_compact_ledger_served(ledger, out_path, max_bytes=max_bytes)[0]


def write_compact_ledger_served(
    ledger: Ledger, out_path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[int, ServedScope | None]:
    """`write_compact_ledger`, also returning the artifact's `served` scope (`None` when every
    DID fit) so a caller can report it without re-reading the file."""
    data, served = _fit(ledger, max_bytes)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(out_path.parent), prefix=f".{out_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(data), served


@dataclass(frozen=True)
class CompactLedger:
    """The compact Worker artifact, loaded into memory: everything `CompactLedger.lookup()` needs
    to answer `/did/{did}` for any DID it covers (see the module docstring). `non_burst` and
    `burst` never share a key -- `load_compact_ledger` enforces it on read, and
    `to_compact_json_bytes` constructs the two disjointly on write.

    NOT guaranteed: this is a snapshot as of `generated_at`, exactly like `ledger.Ledger` -- not a
    live view, and no promise that a DID answered here still posts, or ever will again.
    """

    schema: str
    generated_at: str
    log_rows: int
    posts: int
    dids: int
    bursts: int
    non_burst: Mapping[str, LedgerRow]
    burst: Mapping[str, BurstRecord]
    served: ServedScope | None = None

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_COMPACT:
            raise ValueError(f"schema must be {SCHEMA_COMPACT!r}, got {self.schema!r}")
        if not isinstance(self.generated_at, str) or not self.generated_at:
            raise ValueError("generated_at must be a non-empty string")
        for name, value in (
            ("log_rows", self.log_rows),
            ("posts", self.posts),
            ("dids", self.dids),
            ("bursts", self.bursts),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if self.posts > self.log_rows:
            raise ValueError(f"posts ({self.posts}) cannot exceed log_rows ({self.log_rows})")
        if self.dids > self.posts:
            raise ValueError(f"dids ({self.dids}) cannot exceed posts ({self.posts})")
        if self.bursts > self.dids:
            raise ValueError(f"bursts ({self.bursts}) cannot exceed dids ({self.dids})")
        overlap = set(self.non_burst) & set(self.burst)
        if overlap:
            raise ValueError(f"did(s) present in both non_burst and burst: {sorted(overlap)!r}")
        for did, row in self.non_burst.items():
            if row.facts.did != did or row.score.did != did:
                raise ValueError(
                    f"non_burst[{did!r}] did mismatch: "
                    f"facts.did={row.facts.did!r} score.did={row.score.did!r}"
                )
        total = len(self.non_burst) + len(self.burst)
        if self.served is None:
            if total != self.dids:
                raise ValueError(
                    f"dids ({self.dids}) does not match non_burst+burst count ({total})"
                )
        else:
            if total != self.served.dids:
                raise ValueError(
                    f"served.dids ({self.served.dids}) does not match non_burst+burst count "
                    f"({total})"
                )
            if self.served.dids >= self.dids:
                raise ValueError(
                    f"served.dids ({self.served.dids}) must be below dids ({self.dids}); an "
                    f"artifact holding every DID carries no served scope"
                )
            for did, row in self.non_burst.items():
                if row.facts.last_seen_ts < self.served.last_seen_min_ts:
                    raise ValueError(
                        f"non_burst[{did!r}] last_seen_ts {row.facts.last_seen_ts!r} is before "
                        f"served.last_seen_min_ts {self.served.last_seen_min_ts!r}"
                    )

    def _provenance(self) -> dict[str, object]:
        provenance: dict[str, object] = {
            "ledger_generated_at": self.generated_at,
            "log_rows": self.log_rows,
            "posts": self.posts,
            "dids": self.dids,
            "bursts": self.bursts,
            "schema": self.schema,
        }
        if self.served is not None:
            provenance["served"] = self.served.to_obj()
        return provenance

    def miss(self) -> DidAnswer:
        """The `404` body for a well-formed DID `lookup()` does not know: `{"error":
        "unknown_did"}`, plus the `served` scope when this artifact holds only the most recently
        active DIDs -- so a caller can tell "never posted in the polled rooms" from "last seen
        before the cutoff; see the full ledger"."""
        if self.served is None:
            return {"error": "unknown_did"}
        return {"error": "unknown_did", "served": self.served.to_obj()}

    def lookup(self, did: str) -> DidAnswer | None:
        """The `/did/{did}` response body for `did` (`docs/api.md`'s `GET /did/{did}` contract,
        `handoff/B2-SPEC.md` item 2), or `None` if `did` is absent from both maps -- the caller
        answers `404 {"error": "unknown_did"}` for that case (see `openagentsearch.api.did`).

        A non-burst DID's body carries its full `facts` object and `score.facts_used` verbatim. A
        burst member's body has `"facts": null` (the compact record is all this ledger holds for
        one) and a synthesized `facts_used` reconstructed from the four compact fields alone:
        `[["burst", "true"], ["first_seen_ts", ...], ["post_count", ...],
        ["max_posts_per_minute", ...]]`, plus a top-level `"burst_id"` key a non-burst body never
        carries.
        """
        row = self.non_burst.get(did)
        if row is not None:
            return {
                "did": did,
                "burst": False,
                "score": row.score.score,
                "facts_used": [[name, value] for name, value in row.score.facts_used],
                "facts": facts_to_obj(row.facts),
                "provenance": self._provenance(),
            }
        record = self.burst.get(did)
        if record is not None:
            return {
                "did": did,
                "burst": True,
                "burst_id": record.burst_id,
                "score": 0.0,
                "facts_used": [
                    ["burst", "true"],
                    ["first_seen_ts", _format_number(record.first_seen_ts)],
                    ["post_count", str(record.post_count)],
                    ["max_posts_per_minute", str(record.max_posts_per_minute)],
                ],
                "facts": None,
                "provenance": self._provenance(),
            }
        return None


def _fail(problem: str) -> ValueError:
    return ValueError(f"malformed compact ledger: {problem}")


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string, got {value!r}")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{name} must be an int, got {value!r}")
    return value


def _require_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{name} must be a number, got {value!r}")
    return float(value)


def _obj_to_burst_record(value: object, did: str) -> BurstRecord:
    if not isinstance(value, list) or len(value) != 4:
        raise _fail(f"burst[{did!r}] must be a 4-element array, got {value!r}")
    burst_id_raw, first_seen_ts_raw, post_count_raw, max_ppm_raw = value
    if burst_id_raw is not None and (
        isinstance(burst_id_raw, bool) or not isinstance(burst_id_raw, int)
    ):
        raise _fail(f"burst[{did!r}][0] (burst_id) must be an int or null, got {burst_id_raw!r}")
    return BurstRecord(
        burst_id=burst_id_raw,
        first_seen_ts=_require_number(first_seen_ts_raw, f"burst[{did!r}][1]"),
        post_count=_require_int(post_count_raw, f"burst[{did!r}][2]"),
        max_posts_per_minute=_require_int(max_ppm_raw, f"burst[{did!r}][3]"),
    )


def load_compact_ledger(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> CompactLedger:
    """Fail-closed load of a compact artifact written by `write_compact_ledger`/
    `to_compact_json_bytes`. Raises `ValueError` naming the FIRST problem found, and never returns
    a partially-built `CompactLedger`, for: a file over `max_bytes`; invalid UTF-8; a body that is
    not valid JSON or not an object; a `schema` other than `SCHEMA_COMPACT`; a malformed
    `non_burst`/`burst` entry; a `did` key that disagrees with its own `facts.did`/`score.did`; a
    `did` present in BOTH maps; a header `dids` count that disagrees with `len(non_burst) +
    len(burst)` (or, on a size-fitted artifact, a malformed `served`, a `served.dids` that
    disagrees with that count or is not below `dids`, or a `non_burst` row last seen before
    `served.last_seen_min_ts`).

    NOT guaranteed: this does not re-run `score.score_did` to confirm a stored `Score` is still
    what the formula would produce today -- the same caveat `ledger.load_ledger` documents.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise _fail(f"file is {size} bytes, over the {max_bytes} byte limit")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(f"not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise _fail(f"body must be a JSON object, got {type(obj).__name__}")

    schema = _require_str(obj.get("schema"), "schema")
    if schema != SCHEMA_COMPACT:
        raise _fail(f"schema must be {SCHEMA_COMPACT!r}, got {schema!r}")
    generated_at = _require_str(obj.get("generated_at"), "generated_at")
    log_rows = _require_int(obj.get("log_rows"), "log_rows")
    posts = _require_int(obj.get("posts"), "posts")
    dids = _require_int(obj.get("dids"), "dids")
    bursts = _require_int(obj.get("bursts"), "bursts")

    non_burst_raw = obj.get("non_burst")
    if not isinstance(non_burst_raw, dict):
        raise _fail(f"non_burst must be an object, got {non_burst_raw!r}")
    burst_raw = obj.get("burst")
    if not isinstance(burst_raw, dict):
        raise _fail(f"burst must be an object, got {burst_raw!r}")

    non_burst: dict[str, LedgerRow] = {}
    for did, row_obj in non_burst_raw.items():
        if not isinstance(did, str) or not did:
            raise _fail(f"non_burst key must be a non-empty string, got {did!r}")
        if not isinstance(row_obj, dict):
            raise _fail(f"non_burst[{did!r}] must be an object, got {row_obj!r}")
        facts = obj_to_facts(row_obj.get("facts"))
        score = obj_to_score(row_obj.get("score"))
        if facts.did != did or score.did != did:
            raise _fail(
                f"non_burst[{did!r}] did mismatch: "
                f"facts.did={facts.did!r} score.did={score.did!r}"
            )
        non_burst[did] = LedgerRow(facts=facts, score=score)

    burst: dict[str, BurstRecord] = {}
    for did, record_obj in burst_raw.items():
        if not isinstance(did, str) or not did:
            raise _fail(f"burst key must be a non-empty string, got {did!r}")
        burst[did] = _obj_to_burst_record(record_obj, did)

    served: ServedScope | None = None
    if "served" in obj:
        served_raw = obj["served"]
        if not isinstance(served_raw, dict) or set(served_raw) != {"dids", "last_seen_min_ts"}:
            raise _fail(f"served must be {{dids, last_seen_min_ts}}, got {served_raw!r}")
        served = ServedScope(
            dids=_require_int(served_raw["dids"], "served.dids"),
            last_seen_min_ts=_require_number(
                served_raw["last_seen_min_ts"], "served.last_seen_min_ts"
            ),
        )

    return CompactLedger(
        schema=schema,
        generated_at=generated_at,
        log_rows=log_rows,
        posts=posts,
        dids=dids,
        bursts=bursts,
        non_burst=non_burst,
        burst=burst,
        served=served,
    )
