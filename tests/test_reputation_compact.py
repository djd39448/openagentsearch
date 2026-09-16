"""Package B2, spec item 6: `openagentsearch.reputation.compact` -- round-trip, determinism,
loader rejections, and `CompactLedger.lookup()` against the 20-DID fixture (DID_B is its one
burst member at default settings, per `tests/test_reputation_ledger.py`)."""

import json
from pathlib import Path

import pytest

from openagentsearch.reputation.compact import (
    SCHEMA_COMPACT,
    BurstRecord,
    CompactLedger,
    CompactLedgerSizeError,
    load_compact_ledger,
    to_compact_json_bytes,
    write_compact_ledger,
)
from openagentsearch.reputation.ledger import build_ledger, lookup
from reputation_fixture_support import DID_A, DID_B, NOW, install_fixture


def _build(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    return build_ledger(root, now=NOW)


# --- to_compact_json_bytes: shape + determinism -----------------------------------------------


def test_to_compact_json_bytes_is_deterministic(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    assert to_compact_json_bytes(ledger) == to_compact_json_bytes(ledger)


def test_to_compact_json_bytes_partitions_by_burst_and_covers_every_did(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    assert obj["schema"] == SCHEMA_COMPACT
    assert obj["generated_at"] == ledger.generated_at
    assert obj["log_rows"] == ledger.log_rows
    assert obj["posts"] == ledger.posts
    assert obj["dids"] == ledger.dids == 20
    assert obj["bursts"] == ledger.bursts

    non_burst, burst = obj["non_burst"], obj["burst"]
    assert set(non_burst) & set(burst) == set()
    assert len(non_burst) + len(burst) == ledger.dids
    assert DID_A in non_burst and DID_A not in burst
    assert DID_B in burst and DID_B not in non_burst

    # Every DID in ledger.rows lands in exactly one of the two maps, partitioned by score.burst.
    for row in ledger.rows:
        if row.score.burst:
            assert row.facts.did in burst
        else:
            assert row.facts.did in non_burst

    # non_burst rows carry the exact JSONL row shape (minus the outer "did" key).
    a_row = non_burst[DID_A]
    assert set(a_row) == {"facts", "score"}
    assert a_row["facts"]["did"] == DID_A
    assert a_row["score"]["did"] == DID_A

    # burst rows are exactly the 4-element compact record.
    b_facts = lookup(ledger, DID_B).facts
    assert burst[DID_B] == [
        b_facts.burst_id,
        b_facts.first_seen_ts,
        b_facts.post_count,
        b_facts.max_posts_per_minute,
    ]


def test_no_trailing_newline() -> None:
    # Imported directly as JSON by the Worker (`with { type: "json" }`), like lexical-v1.json --
    # not read line-by-line, so unlike the JSONL ledger this file carries no trailing newline.
    ledger, _report = build_ledger(Path(__file__).resolve().parent, now=NOW)  # empty log_root
    data = to_compact_json_bytes(ledger)
    assert not data.endswith(b"\n")


# --- write_compact_ledger: atomic + size guard --------------------------------------------------


def test_write_compact_ledger_round_trips(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "out" / "ledger-compact.json"
    written = write_compact_ledger(ledger, out_path)
    assert out_path.is_file()
    assert out_path.stat().st_size == written == len(to_compact_json_bytes(ledger))


def test_write_compact_ledger_refuses_oversize_before_any_write(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "out" / "ledger-compact.json"
    with pytest.raises(CompactLedgerSizeError):
        write_compact_ledger(ledger, out_path, max_bytes=1)
    assert not out_path.exists()
    assert not out_path.parent.exists()  # refused before even mkdir


# --- load_compact_ledger: round-trip + fail-closed rejections -----------------------------------


def test_load_compact_ledger_round_trip(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)

    compact = load_compact_ledger(out_path)
    assert compact.schema == SCHEMA_COMPACT
    assert compact.generated_at == ledger.generated_at
    assert compact.dids == ledger.dids == 20
    assert compact.bursts == ledger.bursts
    assert len(compact.non_burst) == 19
    assert len(compact.burst) == 1
    assert compact.non_burst[DID_A].facts == lookup(ledger, DID_A).facts
    assert compact.non_burst[DID_A].score == lookup(ledger, DID_A).score
    assert isinstance(compact.burst[DID_B], BurstRecord)


def test_load_compact_ledger_rejects_oversize_file(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)
    with pytest.raises(ValueError, match="over the .* byte limit"):
        load_compact_ledger(out_path, max_bytes=1)


def test_load_compact_ledger_rejects_non_utf8(tmp_path: Path) -> None:
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_non_json(tmp_path: Path) -> None:
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_non_object(tmp_path: Path) -> None:
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_wrong_schema(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    obj["schema"] = "not-the-right-schema/1"
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="schema must be"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_did_in_both_maps(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    # Move DID_A's non_burst row into `burst` too, so it now appears in both maps.
    obj["burst"][DID_A] = [None, 1.0, 1, 1]
    obj["dids"] = obj["dids"] + 1
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="both non_burst and burst"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_dids_count_mismatch(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    obj["dids"] = obj["dids"] + 1
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match non_burst\\+burst count"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_did_mismatch_in_non_burst_row(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    obj["non_burst"][DID_A]["facts"]["did"] = "did:key:zSomeoneElse00000000"
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="did mismatch"):
        load_compact_ledger(out_path)


def test_load_compact_ledger_rejects_malformed_burst_record(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    obj = json.loads(to_compact_json_bytes(ledger))
    obj["burst"][DID_B] = [None, 1.0, 1]  # only 3 elements, not 4
    out_path = tmp_path / "ledger-compact.json"
    out_path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="4-element array"):
        load_compact_ledger(out_path)


# --- CompactLedger.lookup(): the /did/{did} route body ------------------------------------------


def test_lookup_unknown_did_returns_none(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)
    compact = load_compact_ledger(out_path)
    assert compact.lookup("did:key:zNotInThisFixture00000") is None


def test_lookup_non_burst_did_body_shape(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)
    compact = load_compact_ledger(out_path)

    row = lookup(ledger, DID_A)
    answer = compact.lookup(DID_A)
    assert answer is not None
    assert answer["did"] == DID_A
    assert answer["burst"] is False
    assert answer["score"] == row.score.score
    assert answer["facts_used"] == [list(pair) for pair in row.score.facts_used]
    assert answer["facts"]["first_seen_seq"] == row.facts.first_seen_seq
    assert answer["provenance"] == {
        "ledger_generated_at": ledger.generated_at,
        "log_rows": ledger.log_rows,
        "posts": ledger.posts,
        "dids": ledger.dids,
        "bursts": ledger.bursts,
        "schema": SCHEMA_COMPACT,
    }
    assert "burst_id" not in answer  # only a burst-member body carries this key


def test_lookup_burst_did_body_shape(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    out_path = tmp_path / "ledger-compact.json"
    write_compact_ledger(ledger, out_path)
    compact = load_compact_ledger(out_path)

    facts = lookup(ledger, DID_B).facts
    answer = compact.lookup(DID_B)
    assert answer is not None
    assert answer["did"] == DID_B
    assert answer["burst"] is True
    assert answer["burst_id"] == facts.burst_id
    assert answer["score"] == 0.0
    # DID_B's first_seen_ts in this fixture is a whole-second timestamp (an integral float) --
    # pinned as a literal, not `str(facts.first_seen_ts)`, so this test cannot be tautologically
    # self-consistent with a regression in the formatting itself (see
    # `openagentsearch.reputation.compact._format_number`: Python's `str()` on an integral float
    # keeps a trailing ".0" that JavaScript's `String()` on the Worker side never produces for the
    # same JSON value -- `handoff/B2-SPEC.md` item 2's "one shape everywhere" contract).
    assert facts.first_seen_ts.is_integer()
    assert answer["facts_used"] == [
        ["burst", "true"],
        ["first_seen_ts", str(int(facts.first_seen_ts))],
        ["post_count", str(facts.post_count)],
        ["max_posts_per_minute", str(facts.max_posts_per_minute)],
    ]
    assert answer["facts"] is None


def test_compact_ledger_constructed_directly_validates_dids_count() -> None:
    with pytest.raises(ValueError, match="does not match non_burst\\+burst count"):
        CompactLedger(
            schema=SCHEMA_COMPACT,
            generated_at="2026-01-01T00:00:00Z",
            log_rows=1,
            posts=1,
            dids=1,
            bursts=0,
            non_burst={},
            burst={},
        )


def test_compact_ledger_constructed_directly_validates_did_overlap(tmp_path: Path) -> None:
    ledger, _report = _build(tmp_path)
    row = lookup(ledger, DID_A)
    burst_record = BurstRecord(
        burst_id=None, first_seen_ts=row.facts.first_seen_ts,
        post_count=row.facts.post_count, max_posts_per_minute=row.facts.max_posts_per_minute,
    )
    with pytest.raises(ValueError, match="both non_burst and burst"):
        CompactLedger(
            schema=SCHEMA_COMPACT,
            generated_at="2026-01-01T00:00:00Z",
            log_rows=1,
            posts=1,
            dids=1,
            bursts=0,
            non_burst={DID_A: row},
            burst={DID_A: burst_record},
        )


# --- cross-language "one shape everywhere" (handoff/B2-SPEC.md item 2) -------------------------

COMMITTED_COMPACT_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "reputation" / "compact-fixture.json"
)


def test_lookup_burst_did_facts_used_matches_worker_js_number_formatting() -> None:
    """Loads the SAME committed fixture `worker/test/router.test.mjs` replays
    (`tests/fixtures/reputation/compact-fixture.json`) and asserts `CompactLedger.lookup()`'s
    `first_seen_ts` string for its one burst DID, `did:key:zqMneLidTqoZY`, is exactly what the
    Worker's `String(firstSeenTs)` produces for the identical JSON value (`"1757136000"`, no
    trailing `.0`) -- not Python's native `str(float)` (`"1757136000.0"`). Pins the regression
    `_format_number` fixes so it cannot come back silently on either side.
    """
    compact = load_compact_ledger(COMMITTED_COMPACT_FIXTURE)
    did = "did:key:zqMneLidTqoZY"
    answer = compact.lookup(did)
    assert answer is not None
    assert answer["burst"] is True
    assert answer["facts_used"] == [
        ["burst", "true"],
        ["first_seen_ts", "1757136000"],
        ["post_count", "200"],
        ["max_posts_per_minute", "31"],
    ]
