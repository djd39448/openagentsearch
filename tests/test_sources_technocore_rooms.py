"""Package A3: RoomDirectoryAdapter over a small synthetic rooms.jsonl fixture. No network."""

import hashlib
from pathlib import Path

from openagentsearch.sources.base import AdapterStats
from openagentsearch.sources.technocore_rooms import RoomDirectoryAdapter

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "a3" / "rooms_sample.jsonl"


def _adapter(**kwargs) -> RoomDirectoryAdapter:
    return RoomDirectoryAdapter(FIXTURE, generated_at=1700200000.0, **kwargs)


# 2. Six-line fixture (2 empty/filtered, 1 malformed json, 1 bad id, 2 real-shaped) -----------


def test_six_line_fixture_yields_exactly_two_docs_with_expected_stats():
    adapter = _adapter()
    docs = list(adapter.iter_documents())
    assert adapter.stats() == AdapterStats(
        read=6, yielded=2, skipped_malformed=2, skipped_filtered=2, skipped_too_large=0
    )
    assert [d.url for d in docs] == [
        "https://technocore.chat/r/room-alpha",
        "https://technocore.chat/r/room-beta",
    ]


def test_docs_carry_full_provenance_and_correct_rendering():
    docs = list(_adapter().iter_documents())
    alpha = docs[0]
    assert alpha.title == "Room room-alpha"
    assert alpha.content == (
        "Room room-alpha on technocore.chat. Classification: busy. "
        "First seen 2023-11-14T22:13:20Z; last activity 2023-11-14T23:13:20Z; "
        "messages seen 12; last seq 42. Sample posters: did:plc:aaa, did:plc:bbb."
    )
    prov = alpha.provenance_dict()
    assert prov["room_id"] == "room-alpha"
    assert prov["classification_hint"] == "busy"
    assert prov["first_seen_ts"] == "1700000000"
    assert prov["last_activity_ts"] == "1700003600"
    assert prov["last_seq"] == "42"
    assert prov["message_count_seen"] == "12"
    assert prov["sample_from_dids"] == "did:plc:aaa, did:plc:bbb"
    assert prov["source_kind"] == "room"
    expected_sha256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert prov["source_file_sha256"] == expected_sha256

    beta = docs[1]
    assert "last activity unknown" in beta.content
    assert "last seq unknown" in beta.content
    beta_prov = beta.provenance_dict()
    assert beta_prov["last_activity_ts"] == ""
    assert beta_prov["last_seq"] == ""


# min_messages is respected ---------------------------------------------------------------------


def test_min_messages_is_respected():
    # min_messages=0 lets the two "empty" (message_count_seen=0) rooms through too.
    adapter = _adapter(min_messages=0)
    list(adapter.iter_documents())
    stats = adapter.stats()
    assert stats.yielded == 4
    assert stats.skipped_filtered == 0
    assert stats.skipped_malformed == 2

    # a higher min_messages filters out room-beta (7 messages) too, keeping only room-alpha (12).
    adapter_strict = _adapter(min_messages=10)
    docs_strict = list(adapter_strict.iter_documents())
    assert [d.url for d in docs_strict] == ["https://technocore.chat/r/room-alpha"]
    assert adapter_strict.stats().skipped_filtered == 3


def test_max_rooms_caps_emission():
    adapter = _adapter(max_rooms=1)
    docs = list(adapter.iter_documents())
    assert len(docs) == 1
    assert docs[0].url == "https://technocore.chat/r/room-alpha"


def test_room_id_must_match_the_allowed_pattern():
    # "bad id!" (line 2 of the fixture) contains a space and '!' and must not be indexed,
    # even with min_messages=0 (where every other row in the fixture is emitted).
    docs = list(_adapter(min_messages=0).iter_documents())
    assert not any("bad id" in d.url for d in docs)
    assert len(docs) == 4  # the 2 real rooms + the 2 "empty" ones, but never the bad-id row
