"""Room-id harvesting must never yield ids ending in sentence punctuation.

bin/crawl.py is a script, not a package module, so it is loaded by path. It imports
`requests` at module level; skip cleanly when that is not installed in the test env.
"""

import importlib.util
import pathlib

import pytest

pytest.importorskip("requests")

_CRAWL_PY = pathlib.Path(__file__).resolve().parents[1] / "bin" / "crawl.py"
_spec = importlib.util.spec_from_file_location("oas_crawl", _CRAWL_PY)
crawl = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(crawl)


def test_trailing_punctuation_is_not_part_of_a_room_id():
    text = "see /r/abc-def. then #4979. and room:xyz_1, plus /r/49."
    rooms, _ = crawl.harvest_refs_from_text(text)
    assert "abc-def" in rooms
    assert "4979" in rooms
    assert "xyz_1" in rooms
    assert not any(r[-1] in "._-" for r in rooms)
    # a 2-char token like "49" is below the minimum ref length and is dropped, not mangled
    assert "49." not in rooms


def test_public_room_id_validator_rejects_separator_endings():
    assert crawl._is_public_room_id("abc")
    assert crawl._is_public_room_id("a")
    assert crawl._is_public_room_id("493e3fc050b8800b")
    assert not crawl._is_public_room_id("abc.")
    assert not crawl._is_public_room_id("abc-")
    assert not crawl._is_public_room_id("abc_")
    assert not crawl._is_public_room_id("p-secret")


def test_created_line_extracts_clean_id():
    m = crawl.CREATED_RE.search("<~server> created room-77.")
    assert m is not None
    assert m.group(1) == "room-77"


def test_state_load_drops_invalid_ids_and_flush_rewrites_clean(tmp_path):
    rooms = tmp_path / "rooms.jsonl"
    rooms.write_text(
        '{"id": "goodroom", "message_count_seen": 1}\n'
        '{"id": "0643.", "message_count_seen": 0}\n'
        '{"id": "p-secret", "message_count_seen": 0}\n'
        '{"id": "", "message_count_seen": 0}\n',
        encoding="utf-8",
    )
    state = crawl.State(str(tmp_path))
    state.load()
    assert set(state.rooms) == {"goodroom"}
    state.flush()
    lines = [l for l in rooms.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert '"id": "goodroom"' in lines[0]
