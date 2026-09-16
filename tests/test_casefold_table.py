"""Package C2b, spec Tests item 6: `worker/src/casefold-table.json` is exactly what
`scripts/make_casefold_table.py` generates right now -- never stale, never hand-edited.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "make_casefold_table.py"
TABLE_PATH = REPO / "worker" / "src" / "casefold-table.json"
SUBPROCESS_TIMEOUT = 60


def test_committed_table_matches_a_fresh_generation() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True


def test_table_is_compact_ascii_sorted_and_ends_with_one_lf() -> None:
    raw = TABLE_PATH.read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    text = raw.decode("ascii")  # ensure_ascii=True: the file must contain no raw non-ASCII bytes
    assert "\r" not in text
    assert ": " not in text and ", " not in text  # compact separators, no extra spaces

    obj = json.loads(text)
    assert set(obj.keys()) == {"unicode", "map"}
    assert isinstance(obj["unicode"], str)
    keys = list(obj["map"].keys())
    assert keys == sorted(keys)


def test_every_entry_actually_differs_between_casefold_and_lower() -> None:
    obj = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    for ch, folded in obj["map"].items():
        assert ch.casefold() != ch.lower(), f"{ch!r} does not need a table entry"
        assert ch.casefold() == folded, f"{ch!r} should fold to {folded!r}"


def test_known_examples_are_present() -> None:
    obj = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    table = obj["map"]
    assert table["ß"] == "ss"  # German sharp s
    assert table["µ"] == "μ"  # micro sign -> Greek mu
    assert table["ς"] == "σ"  # Greek final sigma -> sigma
