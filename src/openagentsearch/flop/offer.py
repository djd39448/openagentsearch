"""A fail-closed parsing seam for the FLOP `SessionOffer` object: today it never parses anything.

FLOP maintainer `sv` stated on `flop-labs/yellowpaper#26` (2026-09-14) that the only canonical
signed opening object is the runtime `StandingOffer` / SDK `SessionOffer`, v1 (spot) and v2
(forward), defined in the **private** `flop-labs/flop-core` repository (commit `41d0009`); a
versioned quote/discovery contract will land in the Rust/TypeScript compute-channel SDK later,
with its signed wire shape bound in an appendix of a future yellow paper version. The published
yellow paper v0.5.0 does not contain that shape, and `flop-labs/flop-core` answers 404 to the
public (verified 2026-09-17). sv's comment names, in prose, what the object binds -- see `binds`
on `OfferShapeStatus` -- but that is vocabulary from a public comment, not a schema: no field
types, encodings, ordering, or signing domain are public.

What this module is NOT and does NOT guarantee:

- No parsing. `parse_session_offer` never decodes JSON, never inspects field names or values,
  never guesses at a shape -- it reads only `len(data)` and hashes the raw bytes. A well-formed
  future `SessionOffer` and 64 KiB of random bytes get the exact same answer today.
- No validation and no notion of a valid or invalid offer. There is no accept/reject outcome in
  this module -- see `OFFER_SHAPE_UNPUBLISHED` below. `status == "OFFER_SHAPE_UNPUBLISHED"` must
  never be read as "rejected": it means "cannot be evaluated," not "malformed."
- No signature check of any kind.
- `OfferShapeStatus.binds` is not a schema. It is a tuple of field names taken from sv's prose,
  present so a reader can see what the eventual object is expected to authoritatively bind. When
  the shape is published, `binds` (and this whole module) will be REPLACED to match it, not
  extended in place -- today's `binds` carries no promise about field order, types, or presence
  in the real wire object.
- The result is "unknown," not "rejected." A future package that wires a real parser adds new
  `OfferStatus` values (`"ok"`, `"rejected"`) explicitly; `OFFER_SHAPE_UNPUBLISHED` never silently
  becomes one of those by way of this module changing behavior underneath a caller.
"""

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

OFFER_SHAPE_UNPUBLISHED: Final = "OFFER_SHAPE_UNPUBLISHED"

OfferStatus = Literal["OFFER_SHAPE_UNPUBLISHED"]

_REASON = (
    "the FLOP SessionOffer wire shape (v1 spot / v2 forward) is defined only in the private "
    "flop-labs/flop-core repository; it will be published in an appendix of a future FLOP yellow "
    "paper version (see flop-labs/yellowpaper issue #26), and this parser will be updated then"
)

_BINDS: tuple[str, ...] = (
    "miner",
    "chain_genesis",
    "model_hash",
    "precision",
    "enclave_key",
    "minimum_escrow",
    "sla_bounds",
    "advisory_capacity_hint",
    "expiry",
    "nonce",
    "signature",
    "forward_terms",
)

_WATCH: tuple[str, ...] = (
    "flop-labs/yellowpaper issue #26",
    "flop-labs/flop-core (when public)",
    "Appendix F of a yellow paper version after v0.5.0",
)

_SOURCE = "flop-labs/flop-core@41d0009 (private) — sv, flop-labs/yellowpaper#26, 2026-09-14"


@dataclass(frozen=True)
class OfferShapeStatus:
    """What is known about the `SessionOffer` wire shape today: nothing published. `binds` is
    vocabulary taken from a maintainer's prose comment, not a schema -- see the module docstring.
    """

    published: bool
    source: str
    watch: tuple[str, ...]
    binds: tuple[str, ...]


_OFFER_SHAPE_STATUS = OfferShapeStatus(
    published=False,
    source=_SOURCE,
    watch=_WATCH,
    binds=_BINDS,
)


def offer_shape_status() -> OfferShapeStatus:
    """The one constant `OfferShapeStatus` describing today's state: unpublished. Always returns
    the same object (`offer_shape_status() is offer_shape_status()`)."""
    return _OFFER_SHAPE_STATUS


@dataclass(frozen=True)
class OfferParseResult:
    """The outcome of one `parse_session_offer` call. `status` is always
    `"OFFER_SHAPE_UNPUBLISHED"` today; `reason` names where the real shape will be published;
    `input_bytes`/`input_sha256` describe the input that was given (not its content) so today's
    inputs can be referenced by future fixtures once a real parser exists."""

    status: OfferStatus
    reason: str
    input_bytes: int
    input_sha256: str
    shape: OfferShapeStatus


def parse_session_offer(data: bytes | bytearray, *, max_bytes: int = 65536) -> OfferParseResult:
    """Pure, fail-closed stub for parsing a `SessionOffer`. Never inspects `data`'s content --
    only its length and its sha256 -- and always returns `status ==
    "OFFER_SHAPE_UNPUBLISHED"` (see the module docstring for why: the wire shape is not public).

    `data` must be `bytes` or `bytearray`; anything else raises `TypeError`. `max_bytes` must be a
    positive `int`; a non-positive or non-`int` `max_bytes` raises `ValueError`. `len(data) >
    max_bytes` raises `ValueError` (bounded input, BUILDSPEC §5) -- `len(data) == max_bytes` is
    accepted.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"data must be bytes or bytearray, got {type(data).__name__}")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"max_bytes must be a positive int, got {max_bytes!r}")
    if len(data) > max_bytes:
        raise ValueError(f"data is {len(data)} bytes, exceeds max_bytes={max_bytes}")
    raw = bytes(data)
    return OfferParseResult(
        status=OFFER_SHAPE_UNPUBLISHED,
        reason=_REASON,
        input_bytes=len(raw),
        input_sha256=hashlib.sha256(raw).hexdigest(),
        shape=_OFFER_SHAPE_STATUS,
    )
