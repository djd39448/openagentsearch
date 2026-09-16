"""Package B1, spec item 5: `python -m openagentsearch.reputation.build` as a subprocess.

Everything here is offline; every subprocess call carries a timeout so a bug here can never hang
the suite (same convention as tests/test_lexical_cli.py), and no path is machine-specific (all
come from pytest's `tmp_path`).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from reputation_fixture_support import install_fixture

REPO = Path(__file__).resolve().parents[1]
SUBPROCESS_TIMEOUT = 60


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    return env


def test_cli_writes_the_ledger_and_prints_a_report_line(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 1, stdout_lines
    payload = json.loads(stdout_lines[0])
    assert set(payload.keys()) == {
        "path", "bytes", "log_rows", "posts", "skipped_malformed", "skipped_unsigned",
        "dids", "bursts", "notes_lines", "notes_used", "seconds",
    }
    assert payload["dids"] == 20
    assert payload["posts"] == 238
    assert payload["bytes"] > 0

    assert out_path.is_file()
    assert out_path.stat().st_size == payload["bytes"]
    assert payload["path"] == str(out_path)


def test_cli_defaults_now_to_wall_clock_when_omitted(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert out_path.is_file()


def test_cli_missing_log_root_exits_1_with_json_error(tmp_path: Path):
    missing_root = tmp_path / "does-not-exist"
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(missing_root), "--out", str(out_path),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    error_payload = json.loads(proc.stderr.strip())
    assert "error" in error_payload
    assert not out_path.exists()


def test_cli_missing_required_flag_exits_2(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "openagentsearch.reputation.build", "--out", str(tmp_path)],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout == ""


def test_cli_accepts_room_notes_and_burst_flags(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    notes_path = tmp_path / "notes.jsonl"
    notes_path.write_text(
        json.dumps(
            {"did": "did:key:zAAA", "github_login": "alice", "author": "did:key:zAAA"}
        )
        + "\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--room", "b1", "--notes", str(notes_path),
            "--burst-window", "30", "--burst-min-new", "5",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert payload["notes_lines"] == 1
