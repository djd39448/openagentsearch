"""P6.2: the contribution gate records fail-closed sandbox results."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from scripts.gate import GateResult, run_contribution_gate

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 60.0
GATE_PS1_ORIGINAL_LINES = [
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff check .",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff format --check .",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run mypy src",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run pytest -q",
]


def _write(tmpdir: str, body: str, name: str) -> Path:
    path = Path(tmpdir) / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _records(results_path: Path) -> list[dict]:
    return [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]


def test_benign_contribution_is_recorded_as_passed_but_never_mergeable():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, "print('benign contribution ran')\n", "benign.py")
        results = Path(tmpdir) / "out" / "gate-results.jsonl"
        result = run_contribution_gate(script, timeout=TIMEOUT, results_path=results)
        assert isinstance(result, GateResult)
        assert result.sandbox_passed is True and result.sandbox_returncode == 0
        assert result.reason == "sandbox_passed" and result.mergeable is False
        assert result.contribution_sha256 == hashlib.sha256(script.read_bytes()).hexdigest()
        records = _records(results)
        assert len(records) == 1
        assert records[0] == {
            "contribution_sha256": result.contribution_sha256,
            "sandbox_returncode": 0,
            "sandbox_passed": True,
            "reason": "sandbox_passed",
            "mergeable": False,
        }
        assert list(records[0]) == ["contribution_sha256", "sandbox_returncode", "sandbox_passed", "reason", "mergeable"]


def test_denied_contribution_is_recorded_as_failed_without_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, """
            import socket
            print("LEAK-MARKER-stdout")
            socket.socket()
            """, "denied.py")
        results = Path(tmpdir) / "gate-results.jsonl"
        result = run_contribution_gate(script, timeout=TIMEOUT, results_path=results)
        assert result.sandbox_passed is False and result.mergeable is False
        assert result.sandbox_returncode not in (0, 124)
        assert result.reason == "sandbox_failed"
        raw = results.read_text(encoding="utf-8")
        assert raw.count("\n") == 1
        assert "LEAK-MARKER" not in raw and "SandboxViolation" not in raw and "Traceback" not in raw
        assert json.loads(raw) == {
            "contribution_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "sandbox_returncode": result.sandbox_returncode,
            "sandbox_passed": False,
            "reason": "sandbox_failed",
            "mergeable": False,
        }


def test_timeout_is_recorded_as_sandbox_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, "while True:\n    pass\n", "loop.py")
        results = Path(tmpdir) / "gate-results.jsonl"
        result = run_contribution_gate(script, timeout=1.0, results_path=results)
        assert result.sandbox_returncode == 124
        assert result.reason == "sandbox_timeout"
        assert result.sandbox_passed is False and result.mergeable is False
        assert _records(results)[0]["reason"] == "sandbox_timeout"


def test_records_append_in_invocation_order_with_distinct_hashes():
    with tempfile.TemporaryDirectory() as tmpdir:
        passing = _write(tmpdir, "print('ok')\n", "passing.py")
        failing = _write(tmpdir, "raise SystemExit(3)\n", "failing.py")
        results = Path(tmpdir) / "gate-results.jsonl"
        first = run_contribution_gate(passing, timeout=TIMEOUT, results_path=results)
        second = run_contribution_gate(failing, timeout=TIMEOUT, results_path=results)
        records = _records(results)
        assert len(records) == 2
        assert records[0]["contribution_sha256"] == first.contribution_sha256 == hashlib.sha256(passing.read_bytes()).hexdigest()
        assert records[1]["contribution_sha256"] == second.contribution_sha256 == hashlib.sha256(failing.read_bytes()).hexdigest()
        assert first.contribution_sha256 != second.contribution_sha256
        assert records[0]["reason"] == "sandbox_passed" and records[1]["reason"] == "sandbox_failed"
        assert records[1]["sandbox_returncode"] == 3
        assert records[0]["mergeable"] is False and records[1]["mergeable"] is False


def test_cli_exit_codes_and_gate_script_integration():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO)])
    with tempfile.TemporaryDirectory() as tmpdir:
        benign = _write(tmpdir, "print('benign')\n", "benign.py")
        denied = _write(tmpdir, "import socket\nsocket.socket()\n", "denied.py")
        results = Path(tmpdir) / "cli" / "gate-results.jsonl"
        outputs = []
        for script, expected_code in ((benign, 0), (denied, 1)):
            completed = subprocess.run(
                [sys.executable, "-m", "scripts.gate", "--contribution", str(script), "--results", str(results), "--timeout", "30"],
                cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
            )
            assert completed.returncode == expected_code, completed.stderr
            lines = completed.stdout.splitlines()
            assert len(lines) == 1
            payload = json.loads(lines[0])
            assert set(payload) == {"contribution_sha256", "sandbox_returncode", "sandbox_passed", "reason", "mergeable"}
            assert payload["mergeable"] is False
            assert payload["contribution_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
            assert "benign" not in completed.stdout  # the contribution's own stdout is never relayed
            outputs.append(payload)
        assert outputs[0]["sandbox_passed"] is True and outputs[1]["sandbox_passed"] is False
        assert _records(results) == outputs

    text = (REPO / "scripts" / "gate.ps1").read_text(encoding="utf-8")
    positions = [text.find(line) for line in GATE_PS1_ORIGINAL_LINES]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)
    assert "-m scripts.gate" in text
    assert r"AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe" in text
