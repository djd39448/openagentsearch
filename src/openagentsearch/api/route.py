"""`GET /route` (package D2): routing signals, observations-only -- the same contract on the A2
server (this module) and the Cloudflare Worker (`worker/src/routes.js`'s `/route` branch), see
`docs/api.md`'s `GET /route` section for the full documented shape.

Why this shape and not a real router: BUILDSPEC §3 D2 asks for `/route` to return candidates with
reputation facts, but the only canonical opening object (`SessionOffer` v1/v2) is not public yet
(package D1, `openagentsearch.flop.offer` -- every input answers `OFFER_SHAPE_UNPUBLISHED`), so
there is nothing to parse and nothing to call a "candidate." This route answers the honest shape
instead: `candidates` is always `[]` with a reason, `ranking` is always `None` with a reason (never
ranking across unlike quote units -- flop-labs/yellowpaper#26), and `observations` is what the
surface's own existing search already finds for the queried tokens, each hit joined to the
reputation ledger by the `did:key:` tokens mentioned in its text.

NOT guaranteed: this route does not itself parse or validate any `SessionOffer` -- it never touches
`openagentsearch.flop.offer` beyond reading the one constant `offer_shape_status()` returns.
`dids` extracted from a hit's text are MENTIONS, not authorship or endorsement -- a `did:key:`
token appearing in a document's abstract/snippet says nothing about who wrote it. A ledger body
attached to a mention is evidence from one message log, never an endorsement (see
`openagentsearch.reputation.compact`'s own docstring). `max_latency_ms` is accepted and echoed
back verbatim -- it is never used to filter or rank anything, because no latency facts exist
anywhere in this repository.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from openagentsearch.api.server import JSONRoute, RouteResult
from openagentsearch.flop.offer import offer_shape_status

# Same alphabet for `model_hash` and `precision`; only the length bound differs. Deliberately not
# validated against any fixed vocabulary -- the FLOP `SessionOffer` field vocabulary is unpublished
# (see `openagentsearch.flop.offer`), so a real `model_hash`/`precision` value's actual charset is
# not knowable today.
_MODEL_HASH_RE = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_PRECISION_RE = re.compile(r"^[A-Za-z0-9:_./-]{1,32}$")
_MAX_LATENCY_MS = 600_000
_MIN_K = 1
_MAX_K = 50
_DEFAULT_K = 10

# The `did:key:` method, multibase `z` (base58btc) prefix, 1-120 base58 characters -- the same
# character class `openagentsearch.api.did.DID_RE` anchors as a whole-string match; here it is
# used unanchored to find mentions inside free text (a hit's abstract/snippet), not to validate one
# standalone token.
_DID_FINDALL_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}")

CANDIDATES_REASON = "no SessionOffer shape is public; nothing in this response is an offer"
RANKING_REASON = (
    "no published quote unit; cross-provider ranking is fail-closed (flop-labs/yellowpaper#26)"
)


class ObservationHit(TypedDict):
    """One `observations[]` entry's non-`dids` fields, as an injected `SearchFn` must return them
    -- see `SearchFn`'s own docstring for the exact source of each field on the A2 server."""

    url: str | None
    kind: str | None
    score: float
    text: str


# Pure function, injected: `(query, k) -> hits`, already shaped `{url, kind, score, text}` per
# `ObservationHit` -- keeps this module free of any `VectorStore`/embedder import (BUILDSPEC §5:
# pure parsing, I/O at the edges). On the A2 server this wraps the SAME cosine search `/search`
# uses (`openagentsearch.api.cli`'s `build_server`): `url` is the resolved `doc_url` (nullable,
# exactly like `/search`'s), `kind` is always `None` (the vector store carries no `kind` -- only
# the Worker's lexical index does), `text` is the same 200-character snippet `/search` returns.
SearchFn = Callable[[str, int], list[ObservationHit]]

# `(did) -> body | None`, the exact function `/did/{did}` itself calls (`CompactLedger.lookup`) --
# `None` means "well-formed but absent from the ledger" (this module answers `unknown_did` for
# that), never "no ledger loaded" (that case is `ledger_lookup is None` itself, checked by the
# caller before ever calling this).
LookupFn = Callable[[str], dict[str, object] | None]


@dataclass(frozen=True)
class RouteParams:
    """A successfully parsed `/route` query string."""

    model_hash: str
    precision: str | None
    max_latency_ms: int | None
    k: int


@dataclass(frozen=True)
class RouteError:
    """A failed `/route` query string: `error` is the documented error code, `field` names the
    query parameter that failed -- the route always answers `400 {"error": error, "field": field}`
    for one of these."""

    error: str
    field: str


def parse_route_params(query: dict[str, list[str]]) -> RouteParams | RouteError:
    """Parses and validates a `/route` query string (already `urllib.parse.parse_qs`-shaped, one
    list of raw values per parameter name -- repeated parameters: the first value wins, the same
    convention `openagentsearch.api.search.make_search_route` uses for `q`/`k`).

    Validates in the documented order -- `model_hash`, then `precision`, then `max_latency_ms`,
    then `k` -- returning the FIRST problem found as a `RouteError`, or a `RouteParams` once every
    parameter passes. `model_hash` is checked for emptiness by its length alone (no `.strip()`):
    unlike `/search`'s `q`, a value made entirely of whitespace is already rejected by the
    character-class check below (space is not in the allowed alphabet), so there is nothing extra
    for a strip to catch.
    """
    model_hash_values = query.get("model_hash")
    if not model_hash_values:
        return RouteError("missing_model_hash", "model_hash")
    model_hash = model_hash_values[0]
    if not _MODEL_HASH_RE.fullmatch(model_hash):
        return RouteError("invalid_model_hash", "model_hash")

    precision: str | None = None
    precision_values = query.get("precision")
    if precision_values:
        precision_candidate = precision_values[0]
        if not _PRECISION_RE.fullmatch(precision_candidate):
            return RouteError("invalid_precision", "precision")
        precision = precision_candidate

    max_latency_ms: int | None = None
    max_latency_values = query.get("max_latency_ms")
    if max_latency_values:
        raw = max_latency_values[0]
        if not raw or not all("0" <= c <= "9" for c in raw):
            return RouteError("invalid_max_latency_ms", "max_latency_ms")
        candidate = int(raw)
        if not (1 <= candidate <= _MAX_LATENCY_MS):
            return RouteError("invalid_max_latency_ms", "max_latency_ms")
        max_latency_ms = candidate

    k = _DEFAULT_K
    k_values = query.get("k")
    if k_values:
        k_raw = k_values[0]
        if not k_raw or not all("0" <= c <= "9" for c in k_raw):
            return RouteError("invalid_k", "k")
        k_candidate = int(k_raw)
        if not (_MIN_K <= k_candidate <= _MAX_K):
            return RouteError("invalid_k", "k")
        k = k_candidate

    return RouteParams(
        model_hash=model_hash, precision=precision, max_latency_ms=max_latency_ms, k=k
    )


def extract_dids(text: str, limit: int = 5) -> tuple[str, ...]:
    """Every `did:key:z[1-9A-HJ-NP-Za-km-z]{1,120}` token found in `text`, in order of first
    appearance, de-duplicated, cut at `limit`. A mention only -- this says nothing about who wrote
    `text`, and finding zero DIDs is not an error (an empty tuple)."""
    seen: set[str] = set()
    found: list[str] = []
    for match in _DID_FINDALL_RE.finditer(text):
        did = match.group(0)
        if did in seen:
            continue
        seen.add(did)
        found.append(did)
        if len(found) >= limit:
            break
    return tuple(found)


def _lookup_did_body(ledger_lookup: LookupFn | None, did: str) -> dict[str, object]:
    """The exact `dids[].ledger` value for `did`: `{"error": "ledger_not_built"}` when no ledger
    was loaded at all, `{"error": "unknown_did"}` when a ledger is loaded but does not know `did`,
    or the SAME body `/did/{did}` itself answers for a known `did` -- never re-derived, always
    `ledger_lookup(did)`'s own return value."""
    if ledger_lookup is None:
        return {"error": "ledger_not_built"}
    answer = ledger_lookup(did)
    if answer is None:
        return {"error": "unknown_did"}
    return answer


def make_route_route(
    search: SearchFn,
    ledger_lookup: LookupFn | None,
    index_generated_at: str,
    ledger_generated_at: str | None,
) -> JSONRoute:
    """Builds the `/route` `JSONRoute` (package D2). `search` and `ledger_lookup` are injected
    (see their own docstrings) -- this function and the route it returns import nothing from
    `openagentsearch.vector`/`openagentsearch.embed`.

    `candidates` is always `[]` and `ranking` is always `None` -- an invariant, not a fixture
    accident, while `openagentsearch.flop.offer.offer_shape_status().published` is `False` (see
    the module docstring for why). `offer_shape` is `offer_shape_status()` copied field for field.
    `observations` are the `search(observations_query, k)` hits, each joined to
    `ledger_lookup`/`_lookup_did_body` by the `did:key:` tokens `extract_dids` finds in the hit's
    `text` (at most 5 per hit). EVERY response -- the `400`s included -- carries
    `X-Ledger-Generated-At` exactly when `ledger_generated_at is not None`, the same convention
    `openagentsearch.api.did` uses ("present even for a 400: a ledger IS loaded, so there is
    something to report").
    """
    extra_headers = (
        {} if ledger_generated_at is None else {"X-Ledger-Generated-At": ledger_generated_at}
    )

    def route(query: dict[str, list[str]]) -> RouteResult:
        parsed = parse_route_params(query)
        if isinstance(parsed, RouteError):
            return 400, {"error": parsed.error, "field": parsed.field}, extra_headers

        observations_query = (
            parsed.model_hash
            if parsed.precision is None
            else f"{parsed.model_hash} {parsed.precision}"
        )
        hits = search(observations_query, parsed.k)

        observations: list[dict[str, object]] = []
        for hit in hits:
            text = hit["text"]
            dids = [
                {"did": did, "ledger": _lookup_did_body(ledger_lookup, did)}
                for did in extract_dids(text)
            ]
            observations.append(
                {
                    "url": hit["url"],
                    "kind": hit["kind"],
                    "score": hit["score"],
                    "text": text,
                    "dids": dids,
                }
            )

        shape = offer_shape_status()
        body: dict[str, object] = {
            "query": {
                "model_hash": parsed.model_hash,
                "precision": parsed.precision,
                "max_latency_ms": parsed.max_latency_ms,
                "k": parsed.k,
            },
            "advisory": True,
            "offer_shape": {
                "published": shape.published,
                "source": shape.source,
                "watch": list(shape.watch),
                "binds": list(shape.binds),
            },
            "candidates": [],
            "candidates_reason": CANDIDATES_REASON,
            "ranking": None,
            "ranking_reason": RANKING_REASON,
            "observations": observations,
            "observations_query": observations_query,
            "index_generated_at": index_generated_at,
            "ledger_generated_at": ledger_generated_at,
        }
        return 200, body, extra_headers

    return route
