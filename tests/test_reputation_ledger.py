"""Package B1, ledger.py: build_ledger, to_jsonl_bytes/write_ledger/load_ledger round-trip,
determinism, size refusal, fail-closed load, and lookup()."""

import json
from pathlib import Path

import pytest

from openagentsearch.reputation.facts import PER_DID_BURST_PER_MINUTE_DEFAULT, DidFacts
from openagentsearch.reputation.ledger import (
    SCHEMA,
    Ledger,
    LedgerRow,
    LedgerSizeError,
    build_ledger,
    load_ledger,
    lookup,
    to_jsonl_bytes,
    write_ledger,
)
from openagentsearch.reputation.score import Score

from reputation_fixture_support import NOW, install_fixture, DID_A, DID_B


def _build(tmp_path: Path) -> Ledger:
    root = install_fixture(tmp_path)
    ledger, _report = build_ledger(root, now=NOW)
    return ledger


def test_build_ledger_header_counts_match_the_fixture(tmp_path: Path):
    ledger = _build(tmp_path)
    assert ledger.schema == SCHEMA
    assert ledger.log_rows == 241
    assert ledger.posts == 238
    assert ledger.dids == 20
    assert ledger.bursts == 0  # no first-seen group burst in the 20-DID fixture
    assert len(ledger.rows) == 20


def test_ledger_rows_are_sorted_by_did(tmp_path: Path):
    ledger = _build(tmp_path)
    dids = [row.facts.did for row in ledger.rows]
    assert dids == sorted(dids)


def test_ledger_build_report_matches_ledger_fields(tmp_path: Path):
    root = install_fixture(tmp_path)
    ledger, report = build_ledger(root, now=NOW)
    assert report.log_rows == ledger.log_rows
    assert report.posts == ledger.posts
    assert report.dids == ledger.dids
    assert report.bursts == ledger.bursts
    assert report.skipped_malformed == 1
    assert report.skipped_unsigned == 2
    assert report.seconds >= 0.0


def test_to_jsonl_bytes_is_deterministic_across_two_builds(tmp_path: Path):
    root = install_fixture(tmp_path)
    ledger_1, _ = build_ledger(root, now=NOW)
    ledger_2, _ = build_ledger(root, now=NOW)
    assert to_jsonl_bytes(ledger_1) == to_jsonl_bytes(ledger_2)


def test_to_jsonl_bytes_header_line_shape(tmp_path: Path):
    ledger = _build(tmp_path)
    data = to_jsonl_bytes(ledger)
    lines = data.decode("utf-8").splitlines()
    header = json.loads(lines[0])
    assert set(header) == {"schema", "generated_at", "log_rows", "posts", "dids", "bursts"}
    assert header["schema"] == SCHEMA
    # header + 20 rows
    assert len(lines) == 21
    # rows sorted by did
    row_dids = [json.loads(line)["did"] for line in lines[1:]]
    assert row_dids == sorted(row_dids)


def test_write_then_load_round_trips_to_an_equal_ledger(tmp_path: Path):
    ledger = _build(tmp_path)
    out_path = tmp_path / "out" / "ledger.jsonl"
    written = write_ledger(ledger, out_path)
    assert out_path.is_file()
    assert out_path.stat().st_size == written
    loaded = load_ledger(out_path)
    assert loaded == ledger


def test_write_ledger_is_atomic_no_temp_file_left_behind(tmp_path: Path):
    ledger = _build(tmp_path)
    out_path = tmp_path / "out" / "ledger.jsonl"
    write_ledger(ledger, out_path)
    leftovers = list(out_path.parent.glob(".*.tmp"))
    assert leftovers == []


def test_write_ledger_refuses_oversize_and_writes_nothing(tmp_path: Path):
    ledger = _build(tmp_path)
    out_path = tmp_path / "out" / "ledger.jsonl"
    with pytest.raises(LedgerSizeError):
        write_ledger(ledger, out_path, max_bytes=10)
    assert not out_path.parent.exists()


def test_load_ledger_rejects_wrong_schema(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"schema": "wrong/1", "generated_at": "x", "log_rows": 0, "posts": 0,
                    "dids": 0, "bursts": 0})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema"):
        load_ledger(path)


def test_load_ledger_rejects_unsorted_rows(tmp_path: Path):
    ledger = _build(tmp_path)
    data = to_jsonl_bytes(ledger)
    lines = data.decode("utf-8").splitlines()
    assert len(lines) >= 3
    swapped = [lines[0], lines[2], lines[1], *lines[3:]]
    path = tmp_path / "unsorted.jsonl"
    path.write_text("\n".join(swapped) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sorted"):
        load_ledger(path)


def test_load_ledger_rejects_duplicate_did_row(tmp_path: Path):
    ledger = _build(tmp_path)
    data = to_jsonl_bytes(ledger)
    lines = data.decode("utf-8").splitlines()
    duplicated = [lines[0], lines[1], lines[1], *lines[2:]]
    path = tmp_path / "dup.jsonl"
    path.write_text("\n".join(duplicated) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_ledger(path)


def test_load_ledger_rejects_oversize(tmp_path: Path):
    ledger = _build(tmp_path)
    out_path = tmp_path / "ledger.jsonl"
    write_ledger(ledger, out_path)
    with pytest.raises(ValueError, match="byte limit"):
        load_ledger(out_path, max_bytes=10)


def test_load_ledger_rejects_dids_count_mismatch(tmp_path: Path):
    ledger = _build(tmp_path)
    data = to_jsonl_bytes(ledger)
    lines = data.decode("utf-8").splitlines()
    header = json.loads(lines[0])
    header["dids"] = header["dids"] + 1
    lines[0] = json.dumps(header)
    path = tmp_path / "mismatch.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dids"):
        load_ledger(path)


def test_lookup_found_and_not_found(tmp_path: Path):
    ledger = _build(tmp_path)
    row = lookup(ledger, DID_A)
    assert row is not None
    assert row.facts.did == DID_A
    assert row.score.did == DID_A
    assert lookup(ledger, "did:key:zNotInTheLedgerAtAll") is None


def test_ledger_row_rejects_did_mismatch():
    facts = DidFacts(
        did="did:key:zAAA",
        first_seen_seq=0,
        first_seen_ts=0.0,
        first_seen_room="r",
        last_seen_ts=0.0,
        post_count=1,
        rooms_posted=("r",),
        distinct_text_count=1,
        distinct_text_ratio=1.0,
        outbound_mentions=(),
        inbound_mentions=(),
        unresolved_mentions=0,
        github_login=None,
        github_login_source="",
        burst_id=None,
        max_posts_per_minute=1,
    )
    score = Score(did="did:key:zBBB", score=0.0, burst=False, facts_used=(("x", "y"),))
    with pytest.raises(ValueError):
        LedgerRow(facts=facts, score=score)


def test_build_ledger_passes_burst_params_through(tmp_path: Path):
    root = install_fixture(tmp_path)
    # With a tiny burst window (1s) and min_new=1, distinct first-seen timestamps in the 20-DID
    # fixture group into many more bursts than the default 60s/50-new settings do (which produce
    # bursts == 0, per test_build_ledger_header_counts_match_the_fixture above) -- an exact,
    # concrete count that would fail if burst_window_s/burst_min_new stopped reaching
    # build_facts (e.g. if build_ledger silently ignored them and fell back to the defaults,
    # bursts would come back 0, not 18).
    ledger, _report = build_ledger(
        root, now=NOW, burst_window_s=1.0, burst_min_new=1,
        per_did_burst_per_minute=PER_DID_BURST_PER_MINUTE_DEFAULT,
    )
    assert isinstance(ledger, Ledger)
    assert ledger.bursts == 18


def test_build_ledger_per_did_threshold_reaches_the_scorer(tmp_path: Path):
    """DID B posts 200 times inside a few minutes: a burst member at the default threshold (20 per
    minute), scoring 0.0; with the threshold raised above its rate the same log scores it > 0 --
    proving `per_did_burst_per_minute` is applied at scoring time, not silently dropped."""
    root = install_fixture(tmp_path)
    default_ledger, _ = build_ledger(root, now=NOW)
    row_default = lookup(default_ledger, DID_B)
    assert row_default is not None
    assert row_default.score.burst is True and row_default.score.score == 0.0
    relaxed_ledger, _ = build_ledger(root, now=NOW, per_did_burst_per_minute=10_000)
    row_relaxed = lookup(relaxed_ledger, DID_B)
    assert row_relaxed is not None
    assert row_relaxed.score.burst is False and row_relaxed.score.score > 0.0
