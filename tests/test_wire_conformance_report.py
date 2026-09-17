"""Package D4a: `tests/fixtures/flop/wire-format-v1-report.json` is exactly what
`python scripts/make_wire_conformance_report.py --corpus tests/fixtures/flop/wire-format-v1.json`
generates right now -- never stale, never hand-edited. Same generate-and-diff-check convention
`tests/test_casefold_table.py` uses for `worker/src/casefold-table.json`, adapted for a script that
takes `--out PATH` rather than `--check`. See `docs/flop-wire.md`'s "Conformance report" section.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_flop_wire_corpus import CORPUS, CORPUS_SHA256

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "make_wire_conformance_report.py"
CORPUS_PATH = REPO / "tests" / "fixtures" / "flop" / "wire-format-v1.json"
REPORT_PATH = REPO / "tests" / "fixtures" / "flop" / "wire-format-v1-report.json"
SUBPROCESS_TIMEOUT = 60


def _committed_report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_committed_report_matches_a_fresh_generation(tmp_path: Path) -> None:
    out_path = tmp_path / "wire-format-v1-report.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--corpus", str(CORPUS_PATH), "--out", str(out_path)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out_path.read_bytes() == REPORT_PATH.read_bytes()


def test_report_is_indented_utf8_and_ends_with_one_lf() -> None:
    raw = REPORT_PATH.read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    reparsed = json.loads(text)
    # sort_keys=True, indent=2: re-dumping the parsed object the same way reproduces the file.
    assert json.dumps(reparsed, sort_keys=True, indent=2, ensure_ascii=False) + "\n" == text


def test_summary_counts() -> None:
    report = _committed_report()
    summary = report["summary"]
    assert summary["negative_cases"] == 14
    assert summary["deviations"] == 1
    assert summary["not_verifiable"] == 3
    assert summary["matching"] == 10
    assert report["deviations"][0]["case"] == "wrong_path_orientation"


def test_corpus_sha256_matches_the_pin_test_flop_wire_corpus_already_asserts() -> None:
    report = _committed_report()
    assert report["corpus"]["sha256"] == CORPUS_SHA256


def test_negative_case_ids_are_exactly_the_corpus_ids_once_each() -> None:
    report = _committed_report()
    report_ids = [case["id"] for case in report["negative_cases"]]
    assert len(report_ids) == len(set(report_ids)), "duplicate id in report negative_cases"
    corpus_ids = {case["id"] for case in CORPUS["negative_cases"]}
    assert set(report_ids) == corpus_ids


def _walk_keys(obj: object) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_no_key_contains_the_forbidden_substring() -> None:
    report = _committed_report()
    for key in _walk_keys(report):
        assert "E.54" not in key
