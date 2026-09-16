"""Package ML: parse_room_page, MessageLog.append, select_rooms, RoomMessagesAdapter.

Pure parsing plus on-disk state under tmp_path fixtures only; no network, nothing hangs.
"""

import json
from pathlib import Path

import pytest

from openagentsearch.sources.base import AdapterStats
from openagentsearch.sources.technocore_messages import (
    MessageLog,
    RoomMessagesAdapter,
    parse_room_page,
    select_rooms,
)


def _payload(room="room-x", first_seq=10, last_seq=12, messages=None):
    if messages is None:
        messages = [
            {
                "seq": 10, "ts": "2026-01-01T00:00:00Z", "from": "did:key:zAAA",
                "text": "hello", "sig": "sig1", "nonce": "nonce1",
            },
            {"seq": 11, "ts": "2026-01-01T00:00:01Z", "from": "did:key:zBBB", "text": "world"},
            {"seq": 12, "ts": "2026-01-01T00:00:02Z", "from": "did:key:zAAA", "text": ""},
        ]
    return json.dumps(
        {
            "room": room, "count": len(messages), "first_seq": first_seq, "last_seq": last_seq,
            "generation": 1, "messages": messages,
        }
    ).encode("utf-8")


def _write_rooms_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# ============================================================== 1. parse_room_page


def test_documented_payload_shape_parses_and_defaults_missing_sig_nonce():
    page = parse_room_page(_payload(), room="room-x", observed_at=1700000000.0)
    assert page.room == "room-x"
    assert page.first_seq == 10
    assert page.last_seq == 12
    assert page.skipped_malformed == 0
    assert [m.seq for m in page.messages] == [10, 11, 12]
    assert page.messages[0].sig == "sig1"
    assert page.messages[0].nonce == "nonce1"
    assert page.messages[1].sig == ""
    assert page.messages[1].nonce == ""
    assert page.messages[2].text == ""
    assert all(m.observed_at == 1700000000.0 for m in page.messages)


def test_malformed_message_item_is_skipped_and_counted():
    messages = [
        {"seq": 1, "ts": "t1", "from": "did:1", "text": "ok"},
        {"seq": "not-an-int", "ts": "t2", "from": "did:2", "text": "bad seq"},
        "not-even-a-dict",
        {"ts": "t3", "from": "did:3", "text": "missing seq"},
    ]
    page = parse_room_page(_payload(messages=messages), room="room-x", observed_at=1.0)
    assert page.skipped_malformed == 3
    assert [m.seq for m in page.messages] == [1]


def test_messages_are_sorted_by_seq_ascending():
    messages = [
        {"seq": 5, "ts": "t", "from": "did:a", "text": "x"},
        {"seq": 1, "ts": "t", "from": "did:a", "text": "x"},
        {"seq": 3, "ts": "t", "from": "did:a", "text": "x"},
    ]
    page = parse_room_page(_payload(messages=messages), room="room-x", observed_at=1.0)
    assert [m.seq for m in page.messages] == [1, 3, 5]


def test_integer_nonce_is_stored_as_its_decimal_string():
    messages = [
        {
            "seq": 1, "ts": "t1", "from": "did:1", "text": "signed",
            "sig": "sig-a", "nonce": 1789449982039,
        },
    ]
    page = parse_room_page(_payload(messages=messages), room="room-x", observed_at=1.0)
    assert page.skipped_malformed == 0
    assert page.messages[0].nonce == "1789449982039"
    assert isinstance(page.messages[0].nonce, str)


def test_string_nonce_is_unchanged():
    messages = [
        {"seq": 1, "ts": "t1", "from": "did:1", "text": "x", "sig": "s", "nonce": "abc-123"},
    ]
    page = parse_room_page(_payload(messages=messages), room="room-x", observed_at=1.0)
    assert page.messages[0].nonce == "abc-123"


def test_boolean_and_float_nonce_are_skipped_and_counted():
    messages = [
        {"seq": 1, "ts": "t1", "from": "did:1", "text": "bool nonce", "nonce": True},
        {"seq": 2, "ts": "t2", "from": "did:2", "text": "float nonce", "nonce": 1.5},
        {"seq": 3, "ts": "t3", "from": "did:3", "text": "ok, no nonce at all"},
    ]
    page = parse_room_page(_payload(messages=messages), room="room-x", observed_at=1.0)
    assert page.skipped_malformed == 2
    assert [m.seq for m in page.messages] == [3]
    assert page.messages[0].nonce == ""


def test_live_service_shaped_page_with_integer_nonces_and_some_unsigned_yields_every_item():
    # Reproduces the live builders-room shape that caused package ML's data loss: most items
    # carry a string sig and an integer nonce, a few carry neither key at all.
    messages = [
        {
            "seq": n, "ts": f"t{n}", "from": f"did:key:z{n}", "text": f"msg {n}",
            "sig": f"sig{n}", "nonce": 1789449982000 + n,
        }
        for n in range(1, 195)
    ] + [
        {"seq": n, "ts": f"t{n}", "from": f"did:key:z{n}", "text": f"msg {n}"}
        for n in range(195, 201)
    ]
    page = parse_room_page(
        _payload(first_seq=1, last_seq=200, messages=messages), room="room-x", observed_at=1.0,
    )
    assert page.skipped_malformed == 0
    assert len(page.messages) == 200
    assert page.messages[0].nonce == "1789449982001"
    assert page.messages[0].sig == "sig1"
    assert page.messages[-1].nonce == ""
    assert page.messages[-1].sig == ""


def test_wrong_room_field_raises():
    with pytest.raises(ValueError):
        parse_room_page(_payload(room="other-room"), room="room-x", observed_at=1.0)


def test_oversize_payload_raises():
    huge = json.dumps(
        {
            "room": "room-x", "first_seq": None, "last_seq": None, "messages": [],
            "padding": "x" * 5_000_001,
        }
    ).encode("utf-8")
    with pytest.raises(ValueError):
        parse_room_page(huge, room="room-x", observed_at=1.0)


# ================================================================ 2. MessageLog.append


def test_first_page_writes_all_messages(tmp_path):
    log = MessageLog(tmp_path)
    page = parse_room_page(_payload(), room="room-x", observed_at=1700000000.0)
    report = log.append(page)

    assert report.room == "room-x"
    assert report.new == 3
    assert report.duplicates == 0
    assert report.gap is None
    assert log.last_seq("room-x") == 12

    lines = (tmp_path / "messages" / "room-x.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["seq"] == 10
    assert first["sender"] == "did:key:zAAA"
    assert list(first.keys()) == [
        "room", "seq", "ts", "sender", "text", "sig", "nonce", "observed_at",
    ]

    state_path = tmp_path / "message-log-state.json"
    assert state_path.exists()
    assert not (tmp_path / "message-log-state.json.tmp").exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema"] == "openagentsearch.message-log/1"
    assert state["rooms"]["room-x"]["last_seq"] == 12
    assert state["rooms"]["room-x"]["messages"] == 3
    assert state["rooms"]["room-x"]["gaps"] == []


def test_second_overlapping_page_writes_only_new_and_counts_duplicates(tmp_path):
    log = MessageLog(tmp_path)
    log.append(parse_room_page(_payload(), room="room-x", observed_at=1.0))

    overlap_messages = [
        {"seq": 11, "ts": "t11", "from": "did:x", "text": "dup"},
        {"seq": 12, "ts": "t12", "from": "did:x", "text": "dup"},
        {"seq": 13, "ts": "t13", "from": "did:y", "text": "new"},
    ]
    page2 = parse_room_page(
        _payload(first_seq=11, last_seq=13, messages=overlap_messages),
        room="room-x", observed_at=2.0,
    )
    report2 = log.append(page2)

    assert report2.new == 1
    assert report2.duplicates == 2
    assert report2.gap is None
    assert log.last_seq("room-x") == 13

    lines = (tmp_path / "messages" / "room-x.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # 3 from page1 + 1 genuinely new from page2
    assert json.loads(lines[-1])["seq"] == 13


def test_gap_recorded_when_first_seq_skips_ahead_and_log_still_appends(tmp_path):
    log = MessageLog(tmp_path)
    log.append(parse_room_page(_payload(), room="room-x", observed_at=1.0))  # last_seq now 12

    skip_messages = [{"seq": 20, "ts": "t20", "from": "did:z", "text": "skipped ahead"}]
    page = parse_room_page(
        _payload(first_seq=20, last_seq=20, messages=skip_messages),
        room="room-x", observed_at=2.0,
    )
    report = log.append(page)

    assert report.gap == (13, 20)
    assert report.new == 1
    assert log.last_seq("room-x") == 20

    state = json.loads((tmp_path / "message-log-state.json").read_text(encoding="utf-8"))
    assert state["rooms"]["room-x"]["gaps"] == [[13, 20]]
    lines = (tmp_path / "messages" / "room-x.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # the log still appends despite the gap


def test_no_gap_recorded_on_the_very_first_page_for_a_room(tmp_path):
    log = MessageLog(tmp_path)
    page = parse_room_page(_payload(first_seq=500, last_seq=500), room="room-x", observed_at=1.0)
    report = log.append(page)
    assert report.gap is None  # last_seq was the initial -1: nothing to compare against yet


def test_last_seq_persists_across_a_reopened_message_log(tmp_path):
    log1 = MessageLog(tmp_path)
    log1.append(parse_room_page(_payload(), room="room-x", observed_at=1.0))
    assert log1.last_seq("room-x") == 12

    log2 = MessageLog(tmp_path)
    assert log2.last_seq("room-x") == 12
    assert log2.last_seq("never-seen") == -1


def test_last_seq_self_heals_when_state_file_lags_behind_the_physical_jsonl(tmp_path):
    # Simulates a crash/kill (or an exception inside _write_state, e.g. disk full) that lands
    # between the jsonl write and the atomic state replace: the .jsonl already has seq 1-5, but
    # the state file never recorded it (here: deleted outright, the worst case). last_seq() must
    # recover the true high-water mark from the physical file rather than reporting -1, so a
    # resumed poll never re-appends messages the log already has as if they were new.
    log1 = MessageLog(tmp_path)
    first_page = parse_room_page(
        _payload(first_seq=1, last_seq=5, messages=[
            {"seq": n, "ts": f"t{n}", "from": "did:x", "text": "m"} for n in range(1, 6)
        ]),
        room="room-x", observed_at=1.0,
    )
    log1.append(first_page)
    assert log1.last_seq("room-x") == 5

    (tmp_path / "message-log-state.json").unlink()

    log2 = MessageLog(tmp_path)
    assert log2.last_seq("room-x") == 5  # healed from the .jsonl tail, not the (now-missing) state

    resumed_page = parse_room_page(
        _payload(first_seq=1, last_seq=7, messages=[
            {"seq": n, "ts": f"t{n}", "from": "did:x", "text": "m"} for n in range(1, 8)
        ]),
        room="room-x", observed_at=2.0,
    )
    report = log2.append(resumed_page)
    assert report.new == 2  # only 6 and 7 are genuinely new
    assert report.duplicates == 5

    lines = (tmp_path / "messages" / "room-x.jsonl").read_text(encoding="utf-8").splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == [1, 2, 3, 4, 5, 6, 7]  # no seq physically duplicated, still monotonic


# =================================================================== 3. select_rooms


def test_select_rooms_explicit_first_then_top_n_active_excludes_private(tmp_path):
    rooms_path = tmp_path / "rooms.jsonl"
    now = 1_700_100_000.0
    _write_rooms_jsonl(
        rooms_path,
        [
            {"id": "busy-room", "message_count_seen": 500, "last_activity_ts": int(now - 10)},
            {"id": "quiet-room", "message_count_seen": 50, "last_activity_ts": int(now - 20)},
            {
                "id": "stale-room", "message_count_seen": 9000,
                "last_activity_ts": int(now - 1_000_000),
            },
            {"id": "p-secret", "message_count_seen": 999, "last_activity_ts": int(now - 5)},
            {"id": "explicit-room", "message_count_seen": 1, "last_activity_ts": int(now - 5)},
        ],
    )
    result = select_rooms(
        rooms_path, explicit=("explicit-room",), top=2, active_within_s=3600.0, now=now,
    )
    assert result == ("explicit-room", "busy-room", "quiet-room")


def test_select_rooms_explicit_p_room_raises_before_reading_the_file():
    with pytest.raises(ValueError):
        select_rooms(
            Path("this-file-does-not-exist.jsonl"),
            explicit=("p-private",), top=0, active_within_s=1.0, now=0.0,
        )


def test_select_rooms_is_deterministic_and_dedupes_explicit_from_top_n(tmp_path):
    rooms_path = tmp_path / "rooms.jsonl"
    now = 1000.0
    _write_rooms_jsonl(
        rooms_path,
        [
            {"id": "room-a", "message_count_seen": 10, "last_activity_ts": 999},
            {"id": "room-b", "message_count_seen": 10, "last_activity_ts": 999},
        ],
    )
    result1 = select_rooms(rooms_path, explicit=(), top=5, active_within_s=100.0, now=now)
    result2 = select_rooms(rooms_path, explicit=(), top=5, active_within_s=100.0, now=now)
    assert result1 == result2 == ("room-a", "room-b")  # tie on count broken by id ascending

    result3 = select_rooms(rooms_path, explicit=("room-a",), top=5, active_within_s=100.0, now=now)
    assert result3 == ("room-a", "room-b")  # not duplicated


def test_select_rooms_invalid_explicit_id_raises(tmp_path):
    with pytest.raises(ValueError):
        select_rooms(
            tmp_path / "rooms.jsonl", explicit=("has a space",), top=0,
            active_within_s=1.0, now=0.0,
        )


def test_select_rooms_exclude_drops_a_top_n_candidate_and_the_next_one_takes_its_place(tmp_path):
    rooms_path = tmp_path / "rooms.jsonl"
    now = 1_700_100_000.0
    _write_rooms_jsonl(
        rooms_path,
        [
            {"id": "events", "message_count_seen": 500, "last_activity_ts": int(now - 10)},
            {"id": "builders", "message_count_seen": 400, "last_activity_ts": int(now - 10)},
            {"id": "quiet-room", "message_count_seen": 50, "last_activity_ts": int(now - 10)},
        ],
    )
    without_exclude = select_rooms(
        rooms_path, explicit=(), top=2, active_within_s=3600.0, now=now,
    )
    assert without_exclude == ("events", "builders")

    with_exclude = select_rooms(
        rooms_path, explicit=(), top=2, active_within_s=3600.0, now=now, exclude=("events",),
    )
    assert with_exclude == ("builders", "quiet-room")  # quiet-room took events' place


def test_select_rooms_exclude_does_not_affect_an_unrelated_explicit_room():
    result = select_rooms(
        Path("this-file-does-not-exist.jsonl"),
        explicit=("room-a",), top=0, active_within_s=1.0, now=0.0, exclude=("room-b",),
    )
    assert result == ("room-a",)


def test_select_rooms_id_in_both_explicit_and_exclude_raises_before_the_file_is_read():
    with pytest.raises(ValueError):
        select_rooms(
            Path("this-file-does-not-exist.jsonl"),
            explicit=("room-a",), top=5, active_within_s=1.0, now=0.0, exclude=("room-a",),
        )


def test_select_rooms_malformed_exclude_id_raises():
    with pytest.raises(ValueError):
        select_rooms(
            Path("this-file-does-not-exist.jsonl"),
            explicit=(), top=0, active_within_s=1.0, now=0.0, exclude=("has a space",),
        )


# ============================================================ 4. RoomMessagesAdapter


def _seed_log_with_n_messages(root: Path, room: str, n: int) -> None:
    log = MessageLog(root)
    messages = [
        {"seq": i, "ts": f"t{i}", "from": f"did:key:z{i % 3}", "text": f"msg {i}"}
        for i in range(1, n + 1)
    ]
    page = parse_room_page(
        _payload(room=room, first_seq=1, last_seq=n, messages=messages),
        room=room, observed_at=1.0,
    )
    log.append(page)


def test_windows_of_20_over_45_messages_yield_3_docs_with_correct_ranges(tmp_path):
    _seed_log_with_n_messages(tmp_path, "room-x", 45)
    adapter = RoomMessagesAdapter(tmp_path, window=20)
    docs = list(adapter.iter_documents())

    assert len(docs) == 3
    ranges = [
        (d.provenance_dict()["seq_from"], d.provenance_dict()["seq_to"]) for d in docs
    ]
    assert ranges == [("1", "20"), ("21", "40"), ("41", "45")]
    assert docs[0].url == "https://technocore.chat/r/room-x#1-20"
    assert docs[0].title == "Room room-x messages 1-20"
    assert docs[2].provenance_dict()["message_count"] == "5"
    assert "[1] t1 did:key:z1: msg 1" in docs[0].content

    stats = adapter.stats()
    assert stats.yielded == 3
    assert stats.read == 45
    assert stats.skipped_malformed == 0


def test_empty_log_yields_zero_docs(tmp_path):
    (tmp_path / "messages").mkdir()
    adapter = RoomMessagesAdapter(tmp_path)
    docs = list(adapter.iter_documents())
    assert docs == []
    assert adapter.stats() == AdapterStats(0, 0, 0, 0, 0)


def test_no_messages_dir_at_all_yields_zero_docs(tmp_path):
    adapter = RoomMessagesAdapter(tmp_path)
    assert list(adapter.iter_documents()) == []


def test_malformed_and_cross_room_lines_are_skipped_and_counted(tmp_path):
    messages_dir = tmp_path / "messages"
    messages_dir.mkdir()
    good = {
        "room": "room-y", "seq": 1, "ts": "t1", "sender": "did:1", "text": "hi",
        "sig": "", "nonce": "", "observed_at": 1.0,
    }
    other_room = {
        "room": "other-room", "seq": 2, "ts": "t2", "sender": "did:2", "text": "wrong room",
        "sig": "", "nonce": "", "observed_at": 1.0,
    }
    with (messages_dir / "room-y.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(good) + "\n")
        fh.write("not json at all\n")
        fh.write(json.dumps(other_room) + "\n")

    adapter = RoomMessagesAdapter(tmp_path, window=20)
    docs = list(adapter.iter_documents())

    assert len(docs) == 1
    assert docs[0].provenance_dict()["message_count"] == "1"
    stats = adapter.stats()
    assert stats.skipped_malformed == 2
    assert stats.read == 3
