"""Package CT: the compact Worker artifact is size-bounded by construction
(`compact.fit_compact_json_bytes`) -- past its byte budget it keeps exactly the DIDs last seen at or
after a cutoff and says so in `served`; the whole-ledger case stays byte-identical to
`to_compact_json_bytes`. Uses the same 20-DID fixture as `tests/test_reputation_compact.py`."""

import dataclasses
import json
from pathlib import Path

import pytest
from reputation_fixture_support import NOW, install_fixture

from openagentsearch.api.did import make_did_prefix_route
from openagentsearch.api.healthz import _ledger_summary
from openagentsearch.reputation.compact import (
    CompactLedgerSizeError,
    fit_compact_json_bytes,
    load_compact_ledger,
    to_compact_json_bytes,
    write_compact_ledger,
    write_compact_ledger_served,
)
from openagentsearch.reputation.ledger import Ledger, build_ledger


def _build(tmp_path: Path) -> Ledger:
    root = install_fixture(tmp_path / "log")
    ledger, _report = build_ledger(root, now=NOW)
    return ledger


def _last_seen(ledger: Ledger) -> dict[str, float]:
    return {row.facts.did: row.facts.last_seen_ts for row in ledger.rows}


def _kept(data: bytes) -> set[str]:
    obj = json.loads(data)
    return set(obj["non_burst"]) | set(obj["burst"])


def _check_fitted(ledger: Ledger, data: bytes, budget: int) -> dict:
    """Every invariant a fitted artifact promises, checked from the bytes alone."""
    assert len(data) <= budget
    obj = json.loads(data)
    served = obj["served"]
    kept = _kept(data)
    assert served["dids"] == len(kept) < obj["dids"] == ledger.dids
    cutoff = served["last_seen_min_ts"]
    # Exactly "every DID last seen at or after the cutoff" -- no tie at the cutoff is split.
    assert kept == {did for did, ts in _last_seen(ledger).items() if ts >= cutoff}
    return served


def test_whole_ledger_fits_byte_identical_and_no_served(tmp_path: Path) -> None:
    ledger = _build(tmp_path)
    full = to_compact_json_bytes(ledger)
    assert fit_compact_json_bytes(ledger, len(full)) == full
    assert "served" not in json.loads(full)
    _, served = write_compact_ledger_served(ledger, tmp_path / "c.json", max_bytes=len(full))
    assert served is None


def test_budget_sweep_every_fit_is_within_budget_exact_and_maximal(tmp_path: Path) -> None:
    ledger = _build(tmp_path)
    full = to_compact_json_bytes(ledger)
    budget = len(full) - 1
    seen_counts = []
    while True:
        try:
            data = fit_compact_json_bytes(ledger, budget)
        except CompactLedgerSizeError:
            break
        served = _check_fitted(ledger, data, budget)
        # The size arithmetic is exact: the artifact's own length is a fixpoint budget, and one
        # byte less must drop at least one timestamp group.
        assert fit_compact_json_bytes(ledger, len(data)) == data
        try:
            smaller = fit_compact_json_bytes(ledger, len(data) - 1)
        except CompactLedgerSizeError:
            smaller = None
        if smaller is not None:
            assert json.loads(smaller)["served"]["dids"] < served["dids"]
        assert fit_compact_json_bytes(ledger, budget) == data  # deterministic
        seen_counts.append(served["dids"])
        budget = len(data) - 1
    assert seen_counts == sorted(seen_counts, reverse=True)
    assert len(seen_counts) >= 3  # the fixture spans several distinct last-seen times


def test_tie_group_at_the_cutoff_is_kept_or_dropped_whole(tmp_path: Path) -> None:
    ledger = _build(tmp_path)
    rows = sorted(ledger.rows, key=lambda r: (-r.facts.last_seen_ts, r.facts.did))
    # Give the 2nd..4th newest DIDs the same last-seen time, then pick a budget that fits the
    # newest DID plus only part of that tie group.
    tie_ts = rows[1].facts.last_seen_ts
    tied = {r.facts.did for r in rows[1:4]}
    new_rows = tuple(
        dataclasses.replace(r, facts=dataclasses.replace(r.facts, last_seen_ts=tie_ts))
        if r.facts.did in tied
        else r
        for r in ledger.rows
    )
    tied_ledger = dataclasses.replace(ledger, rows=new_rows)
    newest_only = fit_compact_json_bytes(tied_ledger, len(to_compact_json_bytes(tied_ledger)) - 1)
    budgets = range(len(newest_only), 0, -1)
    for budget in budgets:
        try:
            data = fit_compact_json_bytes(tied_ledger, budget)
        except CompactLedgerSizeError:
            break
        kept = _kept(data)
        assert tied <= kept or not (tied & kept)
        _check_fitted(tied_ledger, data, budget)


def test_too_small_budget_raises_and_writes_nothing(tmp_path: Path) -> None:
    ledger = _build(tmp_path)
    out = tmp_path / "out" / "c.json"
    with pytest.raises(CompactLedgerSizeError):
        write_compact_ledger(ledger, out, max_bytes=500)
    assert not out.exists()


def _fitted(tmp_path: Path) -> tuple[Ledger, Path, dict]:
    ledger = _build(tmp_path)
    full = to_compact_json_bytes(ledger)
    out = tmp_path / "fitted.json"
    written, served = write_compact_ledger_served(ledger, out, max_bytes=len(full) * 2 // 3)
    assert served is not None and out.stat().st_size == written
    return ledger, out, served.to_obj()


def test_fitted_artifact_round_trips_and_answers_with_its_scope(tmp_path: Path) -> None:
    ledger, out, served = _fitted(tmp_path)
    loaded = load_compact_ledger(out)
    assert loaded.served is not None and loaded.served.to_obj() == served
    assert loaded.dids == ledger.dids
    whole = load_compact_ledger(_write_whole(ledger, tmp_path))
    kept = _kept(out.read_bytes())
    last_seen = _last_seen(ledger)
    for did in last_seen:
        answer = loaded.lookup(did)
        if did in kept:
            expected = whole.lookup(did)
            assert expected is not None and answer is not None
            assert answer["provenance"] == {**expected["provenance"], "served": served}
            assert {k: v for k, v in answer.items() if k != "provenance"} == {
                k: v for k, v in expected.items() if k != "provenance"
            }
        else:
            assert answer is None
    assert loaded.miss() == {"error": "unknown_did", "served": served}
    assert whole.miss() == {"error": "unknown_did"}
    assert _ledger_summary(loaded) == {
        "dids": ledger.dids, "bursts": ledger.bursts, "generated_at": ledger.generated_at,
        "served": served,
    }
    route = make_did_prefix_route(loaded)
    dropped = min(set(last_seen) - kept)
    status, body, _headers = route(dropped, {})
    assert (status, body) == (404, {"error": "unknown_did", "served": served})


def _write_whole(ledger: Ledger, tmp_path: Path) -> Path:
    path = tmp_path / "whole.json"
    path.write_bytes(to_compact_json_bytes(ledger))
    return path


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda o: o["served"].update(extra=1), "served must be"),
        (lambda o: o["served"].update(dids=o["served"]["dids"] + 1), "served.dids"),
        (lambda o: o.update(dids=o["served"]["dids"]), "must be below dids"),
        (lambda o: o["served"].update(last_seen_min_ts=o["served"]["last_seen_min_ts"] + 1e9),
         "before served.last_seen_min_ts"),
        (lambda o: o["served"].update(dids=True), "served.dids"),
    ],
)
def test_loader_rejects_an_inconsistent_served_scope(tmp_path: Path, mutate, message) -> None:
    _ledger, out, _served = _fitted(tmp_path)
    obj = json.loads(out.read_bytes())
    mutate(obj)
    out.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_compact_ledger(out)
