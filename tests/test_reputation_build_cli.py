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


# ---------------------------------------------------------------------------------------------
# Package B2: --compact-out
# ---------------------------------------------------------------------------------------------


def test_cli_writes_compact_ledger_when_compact_out_given(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    compact_out_path = tmp_path / "out" / "ledger-compact.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--compact-out", str(compact_out_path),
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert set(payload.keys()) == {
        "path", "bytes", "log_rows", "posts", "skipped_malformed", "skipped_unsigned",
        "dids", "bursts", "notes_lines", "notes_used", "seconds", "compact_bytes",
    }
    assert compact_out_path.is_file()
    assert compact_out_path.stat().st_size == payload["compact_bytes"] > 0

    from openagentsearch.reputation.compact import load_compact_ledger

    compact = load_compact_ledger(compact_out_path)
    assert compact.dids == payload["dids"] == 20
    assert compact.bursts == payload["bursts"]


def test_cli_without_compact_out_keeps_the_baseline_report_shape(tmp_path: Path):
    # No "compact_bytes" key at all when --compact-out is omitted -- matches
    # test_cli_writes_the_ledger_and_prints_a_report_line's exact key-set assertion above.
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    compact_out_path = tmp_path / "out" / "ledger-compact.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert "compact_bytes" not in payload
    assert not compact_out_path.exists()


# ---------------------------------------------------------------------------------------------
# Package SN, deliverable 2: --max-ledger-bytes / --max-compact-bytes
# ---------------------------------------------------------------------------------------------


def test_cli_tiny_max_ledger_bytes_fails_with_ledger_size_error_and_writes_nothing(
    tmp_path: Path,
):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--max-ledger-bytes", "10",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    error_payload = json.loads(proc.stderr.strip())
    assert error_payload["error"].startswith("LedgerSizeError:")
    assert not out_path.exists()


def test_cli_tiny_max_compact_bytes_fails_with_ledger_size_error_and_writes_nothing(
    tmp_path: Path,
):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    compact_out_path = tmp_path / "out" / "ledger-compact.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--compact-out", str(compact_out_path), "--max-compact-bytes", "10",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    error_payload = json.loads(proc.stderr.strip())
    assert error_payload["error"].startswith("CompactLedgerSizeError:")
    # --out is written first, under the (untouched, generous) default --max-ledger-bytes, before
    # --compact-out is even attempted -- so the failure here is --compact-out's alone, and --out
    # is left as a normal, complete ledger file, not rolled back.
    assert out_path.is_file()
    assert not compact_out_path.exists()


def test_cli_generous_max_ledger_and_compact_bytes_succeed(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    compact_out_path = tmp_path / "out" / "ledger-compact.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--compact-out", str(compact_out_path),
            "--max-ledger-bytes", "67108864", "--max-compact-bytes", "33554432",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert out_path.is_file() and out_path.stat().st_size == payload["bytes"]
    assert compact_out_path.is_file()


def test_cli_max_ledger_bytes_zero_exits_2(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--max-ledger-bytes", "0",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert not out_path.exists()


def test_cli_max_compact_bytes_zero_exits_2(tmp_path: Path):
    root = install_fixture(tmp_path / "log")
    out_path = tmp_path / "out" / "ledger.jsonl"
    proc = subprocess.run(
        [
            sys.executable, "-m", "openagentsearch.reputation.build",
            "--log-root", str(root), "--out", str(out_path), "--now", "1758000000.0",
            "--max-compact-bytes", "0",
        ],
        cwd=str(REPO), env=_subprocess_env(), capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    assert not out_path.exists()
