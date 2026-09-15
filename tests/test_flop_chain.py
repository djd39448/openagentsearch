"""Package B4: the finality-gated chain-fact ingester seam (`openagentsearch.flop.chain`) --
`ChainFact` / `FinalizedHead` validation, `NullChainSource` and `StaticChainSource` fixtures, and
`ingest_finalized_facts`'s finality gate itself (facts above the finalized head are deferred,
never reach the sink; the finalized head is read exactly once per run)."""

from collections.abc import Iterator

import pytest

from openagentsearch.flop.chain import (
    ChainFact,
    ChainIngestReport,
    FinalizedHead,
    ListFactSink,
    NullChainSource,
    StaticChainSource,
    ingest_finalized_facts,
)


def _fact(height: int, kind: str = "failed_ack", key: str = "k1") -> ChainFact:
    return ChainFact(
        height=height,
        kind=kind,
        key=key,
        payload=(("reason", "timeout"),),
        observed_at=1_700_000_000.0,
    )


# 1. Done-when: only facts at or below the finalized head are ingested, in order --------------


def test_only_facts_at_or_below_finality_are_ingested_in_order():
    source = StaticChainSource(
        finalized=FinalizedHead(height=10, hash_hex="", observed_at=1.0),
        facts=[_fact(8, key="a"), _fact(10, key="b"), _fact(11, key="c"), _fact(12, key="d")],
    )
    sink = ListFactSink()

    report = ingest_finalized_facts(source, since_height=0, sink=sink)

    assert isinstance(report, ChainIngestReport)
    assert [f.height for f in sink.facts()] == [8, 10]
    assert report.deferred_above_finality == 2
    assert all(f.height not in (11, 12) for f in sink.facts())
    assert report.facts == sink.facts()
    assert report.ingested == 2
    assert report.finalized_height == 10


# 2. finalized_head() is read exactly once per run, even if it would answer differently later --


class _MovingHeadSource:
    """A `ChainSource` whose finalized head advances on every call, to prove the ingester reads it
    only once per `ingest_finalized_facts` call."""

    def __init__(self) -> None:
        self.calls = 0
        self._facts = (_fact(5, key="a"), _fact(15, key="b"))

    def finalized_head(self) -> FinalizedHead:
        self.calls += 1
        # First call: height 5. A second call (which must never happen) would report height 20,
        # which would wrongly let the height-15 fact through.
        height = 5 if self.calls == 1 else 20
        return FinalizedHead(height=height, hash_hex="", observed_at=float(self.calls))

    def events_since(self, height: int) -> Iterator[ChainFact]:
        return iter(f for f in self._facts if f.height > height)


def test_finalized_head_is_read_exactly_once():
    source = _MovingHeadSource()
    sink = ListFactSink()

    report = ingest_finalized_facts(source, since_height=0, sink=sink)

    assert source.calls == 1
    assert report.finalized_height == 5
    assert [f.height for f in sink.facts()] == [5]
    assert report.deferred_above_finality == 1


# 3. NullChainSource is a well-defined no-op -----------------------------------------------------


def test_null_chain_source_is_a_no_op():
    sink = ListFactSink()

    report = ingest_finalized_facts(NullChainSource(), since_height=0, sink=sink)

    assert report.finalized_height == 0
    assert report.ingested == 0
    assert report.deferred_above_finality == 0
    assert report.duplicates == 0
    assert report.facts == ()
    assert sink.facts() == ()


def test_null_chain_source_clock_is_injectable():
    source = NullChainSource(clock=lambda: 42.0)
    assert source.finalized_head() == FinalizedHead(height=0, hash_hex="", observed_at=42.0)
    assert list(source.events_since(0)) == []


# 4. Duplicate facts are counted and stored once -------------------------------------------------


def test_duplicate_facts_are_counted_and_stored_once():
    dup = _fact(3, key="dup")
    source = StaticChainSource(
        finalized=FinalizedHead(height=10, hash_hex="", observed_at=1.0),
        facts=[dup],
    )
    sink = ListFactSink()
    sink.accept(dup)  # pre-seed the sink so the ingest run sees a duplicate

    report = ingest_finalized_facts(source, since_height=0, sink=sink)

    assert report.duplicates == 1
    assert report.ingested == 0
    assert len(sink.facts()) == 1


def test_list_fact_sink_rejects_duplicate_by_height_kind_key():
    sink = ListFactSink()
    a = _fact(1, kind="failed_ack", key="k")
    b = _fact(1, kind="failed_ack", key="k")  # same (height, kind, key), different object

    assert sink.accept(a) is True
    assert sink.accept(b) is False
    assert sink.facts() == (a,)


# 5. Validation -----------------------------------------------------------------------------------


def test_chain_fact_rejects_negative_height():
    with pytest.raises(ValueError):
        _fact(-1)


def test_chain_fact_rejects_bool_height():
    with pytest.raises(ValueError):
        ChainFact(height=True, kind="failed_ack", key="k", payload=(), observed_at=1.0)


def test_chain_fact_rejects_bad_kind():
    with pytest.raises(ValueError):
        ChainFact(height=1, kind="FailedAck", key="k", payload=(), observed_at=1.0)


def test_chain_fact_rejects_empty_key():
    with pytest.raises(ValueError):
        ChainFact(height=1, kind="failed_ack", key="", payload=(), observed_at=1.0)


def test_chain_fact_rejects_duplicate_payload_keys():
    with pytest.raises(ValueError):
        ChainFact(
            height=1,
            kind="failed_ack",
            key="k",
            payload=(("a", "1"), ("a", "2")),
            observed_at=1.0,
        )


def test_chain_fact_rejects_unsorted_payload():
    with pytest.raises(ValueError):
        ChainFact(
            height=1,
            kind="failed_ack",
            key="k",
            payload=(("b", "1"), ("a", "2")),
            observed_at=1.0,
        )


def test_chain_fact_rejects_non_finite_observed_at():
    with pytest.raises(ValueError):
        ChainFact(height=1, kind="failed_ack", key="k", payload=(), observed_at=float("nan"))


def test_finalized_head_rejects_bad_hash_hex():
    with pytest.raises(ValueError):
        FinalizedHead(height=1, hash_hex="not-hex", observed_at=1.0)


def test_finalized_head_accepts_empty_hash_hex():
    head = FinalizedHead(height=1, hash_hex="", observed_at=1.0)
    assert head.hash_hex == ""


def test_static_chain_source_rejects_descending_heights():
    with pytest.raises(ValueError):
        StaticChainSource(
            finalized=FinalizedHead(height=10, hash_hex="", observed_at=1.0),
            facts=[_fact(5, key="a"), _fact(3, key="b")],
        )


def test_since_height_rejects_negative():
    source = NullChainSource()
    with pytest.raises(ValueError):
        ingest_finalized_facts(source, since_height=-1, sink=ListFactSink())


def test_since_height_rejects_bool():
    source = NullChainSource()
    with pytest.raises(ValueError):
        ingest_finalized_facts(source, since_height=True, sink=ListFactSink())


# events_since ordering: ascending by (height, kind, key), even when facts share a height --------


def test_static_source_events_since_orders_by_height_kind_key():
    source = StaticChainSource(
        finalized=FinalizedHead(height=10, hash_hex="", observed_at=1.0),
        facts=[
            _fact(5, kind="fraud_verdict", key="z"),
            _fact(5, kind="calibration", key="a"),
            _fact(5, kind="calibration", key="b"),
        ],
    )
    ordered = list(source.events_since(0))
    assert [(f.height, f.kind, f.key) for f in ordered] == [
        (5, "calibration", "a"),
        (5, "calibration", "b"),
        (5, "fraud_verdict", "z"),
    ]


def test_static_source_events_since_excludes_at_or_below_argument():
    source = StaticChainSource(
        finalized=FinalizedHead(height=10, hash_hex="", observed_at=1.0),
        facts=[_fact(3, key="a"), _fact(5, key="b")],
    )
    assert [f.height for f in source.events_since(3)] == [5]
