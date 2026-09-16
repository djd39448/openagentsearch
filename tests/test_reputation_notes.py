"""Package B1, notes.py: the did-* note convention -- self-authored notes used, other-authored
notes ignored, malformed lines counted, a missing path is not an error."""

import json
from pathlib import Path

from openagentsearch.reputation.notes import NotesReport, load_did_notes


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_missing_path_returns_empty_with_a_zeroed_report():
    notes, report = load_did_notes(None)
    assert notes == {}
    assert report == NotesReport(lines=0, used=0, skipped_malformed=0, skipped_not_self_authored=0)


def test_path_that_does_not_exist_returns_empty(tmp_path: Path):
    notes, report = load_did_notes(tmp_path / "does-not-exist.jsonl")
    assert notes == {}
    assert report.lines == 0


def test_self_authored_note_is_used(tmp_path: Path):
    path = tmp_path / "notes.jsonl"
    _write_jsonl(
        path,
        [{"did": "did:key:zAAA", "github_login": "alice", "author": "did:key:zAAA"}],
    )
    notes, report = load_did_notes(path)
    assert notes == {"did:key:zAAA": "alice"}
    assert report.lines == 1
    assert report.used == 1
    assert report.skipped_malformed == 0
    assert report.skipped_not_self_authored == 0


def test_other_authored_note_is_ignored(tmp_path: Path):
    path = tmp_path / "notes.jsonl"
    _write_jsonl(
        path,
        [{"did": "did:key:zAAA", "github_login": "alice", "author": "did:key:zBBB"}],
    )
    notes, report = load_did_notes(path)
    assert notes == {}
    assert report.lines == 1
    assert report.used == 0
    assert report.skipped_not_self_authored == 1


def test_malformed_lines_are_counted_not_raised(tmp_path: Path):
    path = tmp_path / "notes.jsonl"
    path.write_text(
        "\n".join(
            [
                "not json at all",
                json.dumps({"did": "did:key:zAAA"}),  # missing github_login/author
                json.dumps(["not", "an", "object"]),
                json.dumps({"did": "", "github_login": "x", "author": ""}),  # empty strings
                "",  # blank line, not counted at all
                json.dumps(
                    {"did": "did:key:zCCC", "github_login": "carol", "author": "did:key:zCCC"}
                ),
            ]
        ),
        encoding="utf-8",
    )
    notes, report = load_did_notes(path)
    assert notes == {"did:key:zCCC": "carol"}
    assert report.lines == 5
    assert report.used == 1
    assert report.skipped_malformed == 4
    assert report.skipped_not_self_authored == 0


def test_a_later_line_for_the_same_did_overwrites_an_earlier_one(tmp_path: Path):
    path = tmp_path / "notes.jsonl"
    _write_jsonl(
        path,
        [
            {"did": "did:key:zAAA", "github_login": "alice-old", "author": "did:key:zAAA"},
            {"did": "did:key:zAAA", "github_login": "alice-new", "author": "did:key:zAAA"},
        ],
    )
    notes, report = load_did_notes(path)
    assert notes == {"did:key:zAAA": "alice-new"}
    assert report.used == 2
