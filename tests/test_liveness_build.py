"""LM1-SPEC section 7-9: the liveness artifact end to end -- fixture replay pinned byte-for-byte,
determinism, `--room` scoping, size guards, fail-closed loaders, every constant in `method`, the
CLI subprocess (exit codes 0/1/2), and the 08-30 map comparison test.

Fixture regeneration recipe (all from the repo root, with `PYTHONPATH=src`):

```
python -m openagentsearch.reputation.build --log-root tests/fixtures/liveness \\
    --out tests/fixtures/liveness/did-ledger.jsonl --now 1789700000
python -m openagentsearch.liveness.build --log-root tests/fixtures/liveness \\
    --ledger tests/fixtures/liveness/did-ledger.jsonl \\
    --out tests/fixtures/liveness/liveness-v1.expected.json \\
    --compact-out tests/fixtures/liveness/liveness-compact.expected.json \\
    --now 1789700000 --window-days 7
```

The synthetic `messages/*.jsonl` rows and `message-log-state.json` were built by a throwaway
generator script (not committed); the six fixture rooms are shaped so that `contrib-like` -> live,
`infra-like` -> farm, `market-like`/`validators-like` -> quiet, `kibble-like` -> flood (with a
completed work cycle and >=150 rows in one 300-second bucket), and `tiny-like` (10 rows) ->
unknown -- see LM1-SPEC section 9.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openagentsearch.liveness.build import (
    BASELINE_VERDICT_MAP,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_COMPACT_BYTES,
    METHOD,
    CompactLivenessSizeError,
    LivenessSizeError,
    build_liveness,
    compare_to_baseline,
    load_compact_liveness,
    load_liveness,
    to_compact_json_bytes,
    to_json_bytes,
    write_compact_liveness,
    write_liveness,
)
from openagentsearch.liveness.signals import (
    AGENT_MIN_POSTS,
    AGENT_POINTS,
    FARM_BURST_SENDER_SHARE,
    FARM_FAUCET_SENDER_SHARE,
    FARM_MIN_SENDERS,
    FARM_ONE_LINE_SHARE,
    FARM_TEMPLATE_SENDER_SHARE,
    FLOOD_ROWS_PER_5MIN_P95,
    LIVE_MIN_REPLY_SENDERS,
    LIVE_MIN_WORK_CYCLES,
    LIVE_REPLY_SENDER_SHARE,
    ROOM_MIN_ROWS,
    ROOM_MIN_SENDERS,
    SENDER_MAJORITY,
    TEMPLATE_MIN_REPEATS,
    TIER_LIKELY_MIN,
    TIER_LIVE_MIN,
    TIER_WEAK_MIN,
    WINDOW_DAYS_DEFAULT,
)
from openagentsearch.reputation.ledger import build_ledger, load_ledger

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "liveness"
LEDGER_PATH = FIXTURES / "did-ledger.jsonl"
EXPECTED_V1 = FIXTURES / "liveness-v1.expected.json"
EXPECTED_COMPACT = FIXTURES / "liveness-compact.expected.json"
FIXED_NOW = 1789700000.0
SUBPROCESS_TIMEOUT = 60


def _ledger():
    return load_ledger(LEDGER_PATH)


def test_fixture_build_matches_the_pinned_artifact_byte_for_byte():
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    assert to_json_bytes(liveness) == EXPECTED_V1.read_bytes()
    assert to_compact_json_bytes(liveness) == EXPECTED_COMPACT.read_bytes()


def test_fixture_room_classes_match_lm1_spec_section_9_shapes():
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    classes = {room: entry.verdict.class_ for room, entry in liveness.rooms.items()}
    assert classes == {
        "contrib-like": "live",
        "infra-like": "farm",
        "market-like": "quiet",
        "validators-like": "quiet",
        "kibble-like": "flood",
        "tiny-like": "unknown",
    }
    kibble = liveness.rooms["kibble-like"]
    assert kibble.verdict.signals.live is True
    assert kibble.window.gaps_recorded == 1
    assert kibble.all_time.work_cycles_completed >= 1
    assert kibble.all_time.rows_per_5min_p95 >= FLOOD_ROWS_PER_5MIN_P95


def test_two_builds_of_the_same_input_are_byte_identical():
    ledger = _ledger()
    m1 = build_liveness(FIXTURES, ledger, now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    m2 = build_liveness(FIXTURES, ledger, now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    assert to_json_bytes(m1) == to_json_bytes(m2)
    assert to_compact_json_bytes(m1) == to_compact_json_bytes(m2)


def test_room_scoping_restricts_rooms_and_agents():
    liveness = build_liveness(
        FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT,
        rooms=("contrib-like",),
    )
    assert set(liveness.rooms) == {"contrib-like"}
    assert liveness.rooms_read == 1
    # every agent present posted in contrib-like only (scope excludes every other room's senders)
    for entry in liveness.agents.values():
        assert set(r for r, _c in entry.signals.rooms) <= {"contrib-like"}


def test_write_liveness_round_trips_through_load_liveness(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "out" / "liveness-v1.json"
    written = write_liveness(liveness, out)
    assert out.stat().st_size == written
    loaded = load_liveness(out)
    assert to_json_bytes(loaded) == to_json_bytes(liveness)


def test_write_liveness_is_atomic_no_temp_file_left_behind(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "out" / "liveness-v1.json"
    write_liveness(liveness, out)
    assert list(out.parent.glob(".*.tmp")) == []


def test_write_liveness_refuses_oversize_and_writes_nothing(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "out" / "liveness-v1.json"
    with pytest.raises(LivenessSizeError):
        write_liveness(liveness, out, max_bytes=10)
    assert not out.parent.exists()


def test_write_compact_liveness_refuses_oversize_and_writes_nothing(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "out" / "liveness-compact.json"
    with pytest.raises(CompactLivenessSizeError):
        write_compact_liveness(liveness, out, max_bytes=10)
    assert not out.parent.exists()


def test_default_size_guards_accept_the_fixture_build(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "liveness-v1.json"
    compact_out = tmp_path / "liveness-compact.json"
    assert write_liveness(liveness, out, max_bytes=DEFAULT_MAX_BYTES) > 0
    assert write_compact_liveness(liveness, compact_out, max_bytes=DEFAULT_MAX_COMPACT_BYTES) > 0


def test_load_liveness_rejects_wrong_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema": "wrong/1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_liveness(path)


def test_load_liveness_rejects_oversize(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "liveness-v1.json"
    write_liveness(liveness, out)
    with pytest.raises(ValueError, match="byte limit"):
        load_liveness(out, max_bytes=10)


def test_load_liveness_rejects_counts_mismatch(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    obj = json.loads(to_json_bytes(liveness).decode("utf-8"))
    obj["counts"]["rooms_by_class"]["live"] = obj["counts"]["rooms_by_class"].get("live", 0) + 1
    path = tmp_path / "mismatch.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="counts"):
        load_liveness(path)


def test_load_liveness_rejects_class_outside_vocabulary(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    obj = json.loads(to_json_bytes(liveness).decode("utf-8"))
    some_room = next(iter(obj["rooms"]))
    obj["rooms"][some_room]["class"] = "not-a-real-class"
    path = tmp_path / "badclass.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError):
        load_liveness(path)


def test_load_liveness_rejects_tier_outside_vocabulary(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    obj = json.loads(to_json_bytes(liveness).decode("utf-8"))
    some_did = next(iter(obj["agents"]))
    obj["agents"][some_did]["tier"] = "not-a-real-tier"
    # fix "used" points sum isn't checked before tier, so this alone should already fail
    path = tmp_path / "badtier.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError):
        load_liveness(path)


def test_load_liveness_rejects_non_object():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "notobj.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="object"):
            load_liveness(path)


def test_load_compact_liveness_round_trips(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    out = tmp_path / "liveness-compact.json"
    write_compact_liveness(liveness, out)
    loaded = load_compact_liveness(out)
    assert loaded.schema.endswith("compact/1")
    assert set(loaded.rooms) == set(liveness.rooms)
    assert set(loaded.agents) == set(liveness.agents)
    for did, entry in loaded.agents.items():
        assert entry.tier == liveness.agents[did].verdict.tier
        assert entry.points == liveness.agents[did].verdict.points


def test_load_compact_liveness_rejects_wrong_array_length(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    obj = json.loads(to_compact_json_bytes(liveness).decode("utf-8"))
    some_did = next(iter(obj["agents"]))
    obj["agents"][some_did] = obj["agents"][some_did][:11]  # drop one element
    path = tmp_path / "shortarray.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="12-element"):
        load_compact_liveness(path)


def test_load_compact_liveness_rejects_counts_mismatch(tmp_path):
    liveness = build_liveness(FIXTURES, _ledger(), now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT)
    obj = json.loads(to_compact_json_bytes(liveness).decode("utf-8"))
    obj["counts"]["agents_by_tier"]["farm"] = obj["counts"]["agents_by_tier"].get("farm", 0) + 1
    path = tmp_path / "mismatch.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="counts"):
        load_compact_liveness(path)


def test_every_spec_constant_appears_verbatim_in_method():
    constants = METHOD["constants"]
    expected = {
        "WINDOW_DAYS_DEFAULT": WINDOW_DAYS_DEFAULT,
        "ROOM_MIN_ROWS": ROOM_MIN_ROWS,
        "ROOM_MIN_SENDERS": ROOM_MIN_SENDERS,
        "FLOOD_ROWS_PER_5MIN_P95": FLOOD_ROWS_PER_5MIN_P95,
        "FARM_MIN_SENDERS": FARM_MIN_SENDERS,
        "FARM_ONE_LINE_SHARE": FARM_ONE_LINE_SHARE,
        "FARM_TEMPLATE_SENDER_SHARE": FARM_TEMPLATE_SENDER_SHARE,
        "FARM_FAUCET_SENDER_SHARE": FARM_FAUCET_SENDER_SHARE,
        "FARM_BURST_SENDER_SHARE": FARM_BURST_SENDER_SHARE,
        "LIVE_MIN_REPLY_SENDERS": LIVE_MIN_REPLY_SENDERS,
        "LIVE_REPLY_SENDER_SHARE": LIVE_REPLY_SENDER_SHARE,
        "LIVE_MIN_WORK_CYCLES": LIVE_MIN_WORK_CYCLES,
        "TEMPLATE_MIN_REPEATS": TEMPLATE_MIN_REPEATS,
        "SENDER_MAJORITY": SENDER_MAJORITY,
        "AGENT_MIN_POSTS": AGENT_MIN_POSTS,
        "AGENT_POINTS": AGENT_POINTS,
        "TIER_LIVE_MIN": TIER_LIVE_MIN,
        "TIER_LIKELY_MIN": TIER_LIKELY_MIN,
        "TIER_WEAK_MIN": TIER_WEAK_MIN,
        "DEFAULT_MAX_BYTES": DEFAULT_MAX_BYTES,
        "DEFAULT_MAX_COMPACT_BYTES": DEFAULT_MAX_COMPACT_BYTES,
        "SCHEMA": "openagentsearch.liveness/1",
        "SCHEMA_COMPACT": "openagentsearch.liveness-compact/1",
    }
    for name, value in expected.items():
        assert name in constants, f"{name} missing from METHOD['constants']"
        assert constants[name] == value, f"{name}: {constants[name]!r} != {value!r}"
    assert set(constants) == set(expected)


def test_method_carries_patterns_and_tables():
    assert len(METHOD["faucet_onboarding_patterns"]) == 11
    assert "KIBBLE" in METHOD["kibble_line_pattern"] or "JOB" in METHOD["kibble_line_pattern"]
    assert METHOD["reply_head_pattern"]
    assert METHOD["room_table"]
    assert METHOD["agent_points"] == AGENT_POINTS
    assert METHOD["tier_thresholds"] == {
        "live": TIER_LIVE_MIN, "likely_live": TIER_LIKELY_MIN, "weak": TIER_WEAK_MIN,
    }


# ---------------------------------------------------------------------------------------------
# CLI subprocess.
# ---------------------------------------------------------------------------------------------


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return env


def test_cli_writes_liveness_and_prints_a_report_line(tmp_path):
    out = tmp_path / "out" / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(FIXTURES), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--now", str(FIXED_NOW), "--window-days", "7",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload.keys()) == {
        "path", "bytes", "log_rows", "signed_rows", "rooms", "agents", "agents_not_in_ledger",
        "rooms_by_class", "agents_by_tier", "seconds",
    }
    assert out.is_file()
    assert out.stat().st_size == payload["bytes"]


def test_cli_with_compact_out(tmp_path):
    out = tmp_path / "out" / "liveness-v1.json"
    compact_out = tmp_path / "out" / "liveness-compact.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(FIXTURES), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--compact-out", str(compact_out), "--now", str(FIXED_NOW),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert "compact_bytes" in payload
    assert compact_out.is_file()


def test_cli_missing_log_root_exits_1_with_json_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    out = tmp_path / "out" / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(missing), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--now", str(FIXED_NOW),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    error_payload = json.loads(proc.stderr.strip())
    assert "error" in error_payload
    assert not out.exists()


def test_cli_missing_required_flag_exits_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "openagentsearch.liveness.build", "--out", str(tmp_path)],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout == ""


def test_cli_room_flag_restricts_scope(tmp_path):
    out = tmp_path / "out" / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(FIXTURES), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--now", str(FIXED_NOW), "--room", "infra-like",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert payload["rooms"] == 1
    assert payload["rooms_by_class"] == {"farm": 1}


def test_cli_via_python_dash_m_openagentsearch_liveness(tmp_path):
    # `__main__.py` delegates to `build.main()` -- exercised as `python -m openagentsearch.liveness`.
    out = tmp_path / "out" / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness",
            "--log-root", str(FIXTURES), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--now", str(FIXED_NOW),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert out.is_file()


def test_cli_tiny_max_bytes_fails_with_size_error(tmp_path):
    out = tmp_path / "out" / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(FIXTURES), "--ledger", str(LEDGER_PATH), "--out", str(out),
            "--now", str(FIXED_NOW), "--max-bytes", "10",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    error_payload = json.loads(proc.stderr.strip())
    assert error_payload["error"].startswith("LivenessSizeError:")
    assert not out.exists()


# ---------------------------------------------------------------------------------------------
# Comparison test: agreement against the 08-30 map, over the (unrelated, tiny) reputation
# fixture log. Verdicts are expected to drift -- LM1-SPEC section 9 asks only for two reported
# integers >= 0, never a specific agreement value.
# ---------------------------------------------------------------------------------------------

MAP_2026_08_30 = FIXTURES / "technocore-index-2026-08-30.json"
REPUTATION_LOG_SNAPSHOT = REPO / "tests" / "fixtures" / "reputation" / "log-snapshot"


def test_comparison_against_08_30_map_reports_counts_and_pairs():
    """`compare_to_baseline` over the (unrelated, tiny) reputation fixture log: the overlap with
    the real 08-30 map is empty by construction (synthetic rooms/DIDs), so this test pins the
    SHAPE of the comparison and that every count is a non-negative integer -- never a specific
    agreement value (verdicts drift; the operator run reports the live number under the CLI's
    `compare` key). A second, hand-built baseline exercises the mapping and the counting."""
    old_map = json.loads(MAP_2026_08_30.read_text(encoding="utf-8"))
    assert old_map["schema"] == "technocore-chat-map/v1"

    ledger, _report = build_ledger(REPUTATION_LOG_SNAPSHOT, now=FIXED_NOW)
    liveness = build_liveness(
        REPUTATION_LOG_SNAPSHOT, ledger, now=FIXED_NOW, window_days=WINDOW_DAYS_DEFAULT
    )
    result = compare_to_baseline(liveness, old_map)
    assert result["baseline_generated"] == old_map["generated_utc"]
    for key in (
        "rooms_both", "rooms_agree_window", "rooms_agree_all", "agents_both", "agents_agree"
    ):
        assert isinstance(result[key], int) and result[key] >= 0
    assert result["rooms_agree_window"] <= result["rooms_both"]
    assert result["agents_agree"] <= result["agents_both"]
    assert isinstance(result["room_pairs"], list)
    assert isinstance(result["agent_confusion"], dict)
    print(
        f"08-30 comparison: rooms {result['rooms_agree_window']}/{result['rooms_both']} agree "
        f"(window), {result['rooms_agree_all']}/{result['rooms_both']} (all-time); "
        f"agents {result['agents_agree']}/{result['agents_both']} agree"
    )

    # A hand-built baseline over the fixture's own rooms/DIDs: the verdict mapping and counting.
    fixture_rooms = sorted(liveness.rooms)
    fixture_dids = sorted(liveness.agents)
    assert fixture_rooms and fixture_dids
    first_tier = liveness.agents[fixture_dids[0]].verdict.tier
    hand_baseline = {
        "generated_utc": "2026-08-30T19:20:00Z",
        "rooms_examined": [
            {"room": fixture_rooms[0], "verdict": "FARM-DOMINATED"},
            {"room": fixture_rooms[0] + "-not-here", "verdict": "LIVE"},
            {"room": "no-verdict-key"},
        ],
        "agents": {"roster": [{"did": fixture_dids[0], "tier": first_tier}]},
    }
    hand = compare_to_baseline(liveness, hand_baseline)
    assert hand["rooms_both"] == 1
    assert hand["room_pairs"][0][0] == fixture_rooms[0]
    assert hand["room_pairs"][0][3] == "farm" and hand["room_pairs"][0][4] == "FARM-DOMINATED"
    expected_agree = 1 if liveness.rooms[fixture_rooms[0]].verdict.class_ == "farm" else 0
    assert hand["rooms_agree_window"] == expected_agree
    assert hand["agents_both"] == 1 and hand["agents_agree"] == 1
    assert list(hand["agent_confusion"]) == [f"{first_tier}->{first_tier}"]
    assert BASELINE_VERDICT_MAP["LIVE-QUIET"] == "quiet"
    # An unmapped verdict compares as its lowercase self and can never agree with a real class.
    odd = compare_to_baseline(
        liveness, {"rooms_examined": [{"room": fixture_rooms[0], "verdict": "ODD"}]}
    )
    assert odd["room_pairs"][0][3] == "odd" and odd["rooms_agree_window"] == 0
    assert compare_to_baseline(liveness, {}) == {
        "baseline_generated": None, "rooms_both": 0, "rooms_agree_window": 0,
        "rooms_agree_all": 0, "room_pairs": [], "agents_both": 0, "agents_agree": 0,
        "agent_confusion": {},
    }


def _fixture_ledger_for_snapshot(tmp_path: Path) -> Path:
    from openagentsearch.reputation.ledger import write_ledger

    ledger, _report = build_ledger(REPUTATION_LOG_SNAPSHOT, now=FIXED_NOW)
    path = tmp_path / "snapshot-ledger.jsonl"
    write_ledger(ledger, path)
    return path


def test_cli_compare_flag_reports_the_comparison(tmp_path):
    out = tmp_path / "liveness-v1.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.liveness.build",
            "--log-root", str(REPUTATION_LOG_SNAPSHOT),
            "--ledger", str(_fixture_ledger_for_snapshot(tmp_path)),
            "--out", str(out), "--now", str(FIXED_NOW), "--compare", str(MAP_2026_08_30),
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=SUBPROCESS_TIMEOUT,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")}, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip())
    assert report["compare"]["rooms_both"] == 0 and report["compare"]["agents_both"] == 0
    assert report["compare"]["baseline_generated"] == "2026-08-30T19:20:00Z"
