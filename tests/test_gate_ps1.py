"""P34: scripts/gate.ps1 must fail closed across every hard check (text assertions + a transformed-copy run)."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "gate.ps1"
UV_LINES = [
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff check .",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run ruff format --check .",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run mypy src",
    r"C:\Users\trustcore-rdp\tools\uv-0.12.3\uv.exe run pytest -q",
]
INIT = "$GateFailed = 0"
SET = "$GateFailed = 1"


def _text() -> str:
    return GATE.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return [line for line in _text().splitlines() if line.strip()]


def test_original_uv_commands_present_once_in_order():
    text = _text()
    positions = [text.find(line) for line in UV_LINES]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)
    assert [text.count(line) for line in UV_LINES] == [1, 1, 1, 1]
    assert _lines()[0].startswith("param(")


def test_every_hard_check_feeds_the_failure_accumulator():
    lines = _lines()
    text = _text()
    assert text.count(INIT) == 1
    assert text.index(INIT) < text.index(UV_LINES[0])
    for uv in UV_LINES:
        following = lines[lines.index(uv) + 1]
        assert "$LASTEXITCODE" in following and SET in following and following.lstrip().startswith("if "), following


def test_sandbox_invocation_is_conditional_and_never_resets_the_accumulator():
    lines = _lines()
    sandbox = [line for line in lines if "-m scripts.gate" in line]
    assert len(sandbox) == 1
    assert sandbox[0].startswith("if ($Contribution)")
    assert "$LASTEXITCODE -ne 0" in sandbox[0] and SET in sandbox[0]
    assert INIT not in sandbox[0]
    after_init = lines[lines.index(INIT) + 1:]
    assert all(INIT not in line for line in after_init)


def test_final_control_flow_and_transformed_copies():
    lines = _lines()
    assert lines[-2] == "if ($GateFailed -ne 0) { exit 1 }"
    assert lines[-1] == "exit 0"

    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("PowerShell is not available to the test process; text assertions already ran")
    original = _text()
    with tempfile.TemporaryDirectory() as tmpdir:
        all_zero = original
        for uv in UV_LINES:
            all_zero = all_zero.replace(uv, "cmd /c exit 0")
        one_failure = original
        for index, uv in enumerate(UV_LINES):
            one_failure = one_failure.replace(uv, "cmd /c exit 7" if index == 2 else "cmd /c exit 0")
        results = {}
        for name, body in (("all_zero.ps1", all_zero), ("one_failure.ps1", one_failure)):
            copy = Path(tmpdir) / name
            copy.write_text(body, encoding="utf-8")
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(copy)],
                cwd=tmpdir, capture_output=True, text=True, timeout=60,
            )
            results[name] = completed.returncode
        assert results == {"all_zero.ps1": 0, "one_failure.ps1": 1}, results
    assert _text() == original  # the repository script was never modified
