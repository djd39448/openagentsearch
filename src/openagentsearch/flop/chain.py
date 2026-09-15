"""A finality-gated seam between the FLOP chain and the index: `ChainFact` / `FinalizedHead` are
the shared shapes, `ChainSource` is the structural contract a future RPC-backed reader will
satisfy, and `ingest_finalized_facts` is the pure orchestration that keeps chain-derived facts out
of the index until they are behind the finalized head (FLOP yellow paper v0.5.0, R5.3e and
Appendix C.2: a fact seen at the tip can be reorged away, so only facts at heights at or below the
finalized head are safe to index).

What this module is NOT and does NOT guarantee:

- No reorg handling below finality: a chain that reorgs a height this module has already reported
  as finalized (which would itself violate the chain's own finality guarantee) is not detected,
  not reconciled, and not this module's problem -- finality, once reported, is trusted as-is.
- No persistence here. `ChainIngestReport` and `FactSink.accept` are the only outputs; nothing in
  this module writes to disk, to the vector store, or to the index manifest. Wiring a `FactSink`
  that persists is a future package's job.
- No RPC. There is no public FLOP RPC yet, so there is no real `ChainSource` implementation here
  either -- only the `ChainSource` / `FactSink` protocols, and two sources for tests and as a
  no-op default: `NullChainSource` (always at height 0, never yields a fact) and
  `StaticChainSource` (an explicit fixture for tests).
- `NullChainSource` is a stub, not a placeholder for "chain data unavailable right now" --
  `ingest_finalized_facts(NullChainSource(), ...)` is a well-defined no-op (an empty report) and
  is expected to stay that way until a real RPC-backed source replaces it.
- Finality is trusted from the source. `ingest_finalized_facts` never second-guesses
  `finalized_head()` -- it reads it once and treats whatever height and hash come back as
  authoritative for that run; a `ChainSource` that lies about finality (reports a height not
  actually final) has no guard here.
"""

import math
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

_KIND_RE = re.compile(r"^[a-z_]+$")
_HASH_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return float(value)


def _validate_non_negative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")


@dataclass(frozen=True)
class ChainFact:
    """One reputation-relevant fact observed on the FLOP chain at a given height (a FailedAck, a
    calibration snapshot, a fraud verdict, ...). `payload` is a tuple of `(key, value)` string
    pairs, sorted ascending and unique by key -- callers build it that way, and `__post_init__`
    enforces it, so two `ChainFact`s with the same fields always compare and hash identically
    regardless of how the caller happened to order the pairs before construction.

    NOT guaranteed: nothing here validates that `height` is a real height that ever existed on the
    chain, that `kind` is a kind the chain actually emits, or that `payload` values are truthful --
    a `ChainFact` is only as trustworthy as the `ChainSource` that produced it.
    """

    height: int
    kind: str
    key: str
    payload: tuple[tuple[str, str], ...]
    observed_at: float

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.height, "height")
        if not isinstance(self.kind, str) or not _KIND_RE.fullmatch(self.kind):
            raise ValueError(f"kind must match {_KIND_RE.pattern!r}, got {self.kind!r}")
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key must be a non-empty string")
        if not isinstance(self.payload, tuple):
            raise ValueError("payload must be a tuple of (str, str) pairs")
        keys: list[str] = []
        for pair in self.payload:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
            ):
                raise ValueError(f"each payload entry must be a (str, str) pair, got {pair!r}")
            keys.append(pair[0])
        if len(keys) != len(set(keys)):
            raise ValueError(f"payload keys must be unique, got {keys!r}")
        if list(self.payload) != sorted(self.payload):
            raise ValueError(f"payload must be sorted ascending, got {self.payload!r}")
        observed_at = _validate_finite_number(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True)
class FinalizedHead:
    """The chain's finalized head as of one read: `height` and, when the source can supply it, a
    64-lowercase-hex `hash_hex` -- `""` when it cannot (for example `NullChainSource`, which has
    no real chain to hash). NOT guaranteed: no claim that `height` is still final by the time a
    caller acts on it -- see the module docstring's reorg note."""

    height: int
    hash_hex: str
    observed_at: float

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.height, "height")
        if not isinstance(self.hash_hex, str) or not (
            self.hash_hex == "" or _HASH_HEX_RE.fullmatch(self.hash_hex)
        ):
            raise ValueError(
                f"hash_hex must be '' or 64 lowercase hex chars, got {self.hash_hex!r}"
            )
        observed_at = _validate_finite_number(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)


class ChainSource(Protocol):
    """Structural contract a chain reader satisfies by shape, not by inheritance -- there is no
    real implementation of this yet (no public FLOP RPC exists); see `NullChainSource` and
    `StaticChainSource` below.

    NOT guaranteed: `events_since` may return facts ABOVE `finalized_head()`'s height -- filtering
    those out is `ingest_finalized_facts`'s job, never a promise a `ChainSource` makes.
    """

    def finalized_head(self) -> FinalizedHead:
        """The chain's current finalized head, read fresh on every call -- a source is free to
        return a different (higher) head on a later call; nothing here caches."""
        ...

    def events_since(self, height: int) -> Iterator[ChainFact]:
        """Facts with `height` strictly greater than the argument, ascending by
        `(height, kind, key)`. May include facts above the current finalized head -- the caller
        (`ingest_finalized_facts`) is responsible for withholding those."""
        ...


class NullChainSource:
    """The stub `ChainSource` used until a public FLOP RPC exists: always reports a finalized head
    of height 0 with no hash, and never yields a fact. `ingest_finalized_facts` run against this
    source is a well-defined, deterministic no-op -- see the module docstring."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def finalized_head(self) -> FinalizedHead:
        return FinalizedHead(height=0, hash_hex="", observed_at=self._clock())

    def events_since(self, height: int) -> Iterator[ChainFact]:
        return iter(())


class StaticChainSource:
    """A fixed, in-memory `ChainSource` for tests and fixtures: an explicit `finalized` head and a
    fixed sequence of `ChainFact`s. `__init__` validates that the facts are given in non-descending
    height order (raises `ValueError` on descending heights) -- this source does not sort its
    input for you, so a caller who wants deterministic `events_since` ordering across ties in
    height must pass facts already ordered by `(height, kind, key)` (or rely on `events_since`
    re-sorting; it always returns its results sorted that way regardless of input order)."""

    def __init__(self, *, finalized: FinalizedHead, facts: Iterable[ChainFact]) -> None:
        facts_tuple = tuple(facts)
        heights = [fact.height for fact in facts_tuple]
        if heights != sorted(heights):
            raise ValueError(
                f"facts must be given in non-descending height order, got heights {heights!r}"
            )
        self._finalized = finalized
        self._facts = facts_tuple

    def finalized_head(self) -> FinalizedHead:
        return self._finalized

    def events_since(self, height: int) -> Iterator[ChainFact]:
        selected = [fact for fact in self._facts if fact.height > height]
        selected.sort(key=lambda fact: (fact.height, fact.kind, fact.key))
        return iter(selected)


@dataclass(frozen=True)
class ChainIngestReport:
    """The outcome of one `ingest_finalized_facts` run. `facts` holds exactly the facts that
    reached the sink and were accepted (not duplicates), in the order they were offered -- NOT
    every fact `events_since` produced; facts above finality and rejected duplicates are counted
    but never appear here."""

    finalized_height: int
    ingested: int
    deferred_above_finality: int
    duplicates: int
    facts: tuple[ChainFact, ...]


class FactSink(Protocol):
    """Structural contract for whatever `ingest_finalized_facts` hands accepted facts to. NOT
    guaranteed: nothing about persistence, ordering guarantees beyond "called once per accepted
    fact, in order", or thread-safety -- that is entirely the implementation's business."""

    def accept(self, fact: ChainFact) -> bool:
        """Record `fact`. Returns `True` when it was newly recorded, `False` when it was a
        duplicate (already recorded) and this call was a no-op."""
        ...


class ListFactSink:
    """In-memory `FactSink` for tests: keeps every accepted fact in insertion order and rejects a
    duplicate by `(height, kind, key)` (returns `False`, stores nothing a second time)."""

    def __init__(self) -> None:
        self._facts: list[ChainFact] = []
        self._seen: set[tuple[int, str, str]] = set()

    def accept(self, fact: ChainFact) -> bool:
        dedupe_key = (fact.height, fact.kind, fact.key)
        if dedupe_key in self._seen:
            return False
        self._seen.add(dedupe_key)
        self._facts.append(fact)
        return True

    def facts(self) -> tuple[ChainFact, ...]:
        """Every accepted fact, in insertion (acceptance) order."""
        return tuple(self._facts)


def ingest_finalized_facts(
    source: ChainSource, *, since_height: int, sink: FactSink
) -> ChainIngestReport:
    """Pure orchestration behind the chain's finality gate: read `source.finalized_head()` exactly
    ONCE, then offer every fact from `source.events_since(since_height)` to `sink.accept()` in the
    order the source yielded them -- but only when `fact.height <= finalized.height`. A fact above
    the finalized head read at the start of this call is counted in
    `ChainIngestReport.deferred_above_finality` and NEVER reaches `sink`, even if the chain's real
    head has since advanced past it: the head is not re-read mid-run, so a moving head during
    iteration can never widen the window this call ingests against.

    Raises `ValueError` if `since_height` is not a non-negative int.
    """
    _validate_non_negative_int(since_height, "since_height")
    finalized = source.finalized_head()
    ingested_facts: list[ChainFact] = []
    deferred_above_finality = 0
    duplicates = 0
    for fact in source.events_since(since_height):
        if fact.height > finalized.height:
            deferred_above_finality += 1
            continue
        if sink.accept(fact):
            ingested_facts.append(fact)
        else:
            duplicates += 1
    return ChainIngestReport(
        finalized_height=finalized.height,
        ingested=len(ingested_facts),
        deferred_above_finality=deferred_above_finality,
        duplicates=duplicates,
        facts=tuple(ingested_facts),
    )
