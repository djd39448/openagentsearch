"""Package LW: the liveness map lists only agents seen in its own window (the rest are counted in
`agents_outside_window`), and both artifacts are size-bounded by `fit_liveness` -- past the byte
budget the most recently active agents are kept and `served` says so. Uses the LM1 fixture (230
agents, last seen 0.23-1.04 days before FIXED_NOW), so a 0.5-day window splits it."""

import json
from pathlib import Path

import pytest

from openagentsearch.liveness.build import (
    CompactLivenessSizeError,
    LivenessSizeError,
    build_liveness,
    fit_liveness,
    load_compact_liveness,
    load_liveness,
    to_compact_json_bytes,
    to_json_bytes,
    write_liveness,
)
from openagentsearch.reputation.ledger import load_ledger

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "liveness"
FIXED_NOW = 1789700000.0


def _build(window_days: float = 7.0):
    return build_liveness(FIXTURES, load_ledger(FIXTURES / "did-ledger.jsonl"), now=FIXED_NOW,
                          window_days=window_days)


def test_window_lists_only_agents_seen_in_it_and_counts_the_rest():
    whole = _build(7.0)
    assert whole.agents_outside_window == 0
    assert "agents_outside_window" not in json.loads(to_json_bytes(whole))
    half = _build(0.5)
    cutoff = FIXED_NOW - 0.5 * 86400
    assert half.agents and all(e.signals.last_seen_ts >= cutoff for e in half.agents.values())
    assert half.agents_outside_window == len(whole.agents) - len(half.agents) > 0
    left_out = set(whole.agents) - set(half.agents)
    assert all(whole.agents[d].signals.last_seen_ts < cutoff for d in left_out)
    for data in (to_json_bytes(half), to_compact_json_bytes(half)):
        assert json.loads(data)["agents_outside_window"] == half.agents_outside_window


def test_window_maps_round_trip_through_both_loaders(tmp_path: Path):
    half = _build(0.5)
    full, compact = tmp_path / "l.json", tmp_path / "c.json"
    full.write_bytes(to_json_bytes(half))
    compact.write_bytes(to_compact_json_bytes(half))
    assert load_liveness(full).agents_outside_window == half.agents_outside_window
    assert load_compact_liveness(compact).agents_outside_window == half.agents_outside_window


@pytest.mark.parametrize("compact", [False, True])
def test_fit_is_identity_when_it_fits(compact: bool):
    m = _build()
    data = (to_compact_json_bytes if compact else to_json_bytes)(m)
    fitted_data, fitted = fit_liveness(m, len(data), compact=compact)
    assert fitted_data == data and fitted is m


@pytest.mark.parametrize("compact", [False, True])
def test_fit_keeps_exactly_the_newest_agents_within_budget_and_exactly(compact: bool, tmp_path: Path):
    m = _build()
    to_bytes = to_compact_json_bytes if compact else to_json_bytes
    loader = load_compact_liveness if compact else load_liveness
    budget = len(to_bytes(m)) * 2 // 3
    data, fitted = fit_liveness(m, budget, compact=compact)
    assert len(data) <= budget and data == to_bytes(fitted)
    served = fitted.served
    assert served is not None and served["agents"] == len(fitted.agents) < len(m.agents)
    cut = served["last_seen_min_ts"]
    assert set(fitted.agents) == {d for d, e in m.agents.items() if e.signals.last_seen_ts >= cut}
    assert set(fitted.rooms) == set(m.rooms)  # rooms are never dropped
    # Exact: the artifact's own length is a fixpoint budget; one byte less drops a time group.
    assert fit_liveness(m, len(data), compact=compact)[0] == data
    smaller = fit_liveness(m, len(data) - 1, compact=compact)[1]
    assert len(smaller.agents) < len(fitted.agents)
    path = tmp_path / "fitted.json"
    path.write_bytes(data)
    loaded = loader(path)
    assert loaded.served == served and len(loaded.agents) == served["agents"]


def test_too_small_budget_raises_and_writes_nothing(tmp_path: Path):
    m = _build()
    out = tmp_path / "out" / "l.json"
    with pytest.raises(LivenessSizeError):
        write_liveness(m, out, max_bytes=1000)
    assert not out.exists()
    with pytest.raises(CompactLivenessSizeError):
        fit_liveness(m, 1000, compact=True)


def _fitted_obj() -> dict:
    m = _build()
    data, _fitted = fit_liveness(m, len(to_json_bytes(m)) * 2 // 3)
    return json.loads(data)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda o: o["served"].update(agents=o["served"]["agents"] + 1), "served.agents"),
        (lambda o: o["served"].update(extra=1), "served must be"),
        (lambda o: o["served"].update(last_seen_min_ts=o["served"]["last_seen_min_ts"] + 1e6),
         "before served.last_seen_min_ts"),
        (lambda o: o.update(agents_outside_window=0), "only when > 0"),
        (lambda o: o.update(agents_outside_window=-3), "non-negative"),
    ],
)
def test_loader_rejects_inconsistent_window_fields(tmp_path: Path, mutate, message):
    obj = _fitted_obj()
    mutate(obj)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_liveness(path)
