"""Contribution gate: run one untrusted Python contribution in the P6.1 sandbox and record the
result fail-closed (P6.2).

Usage: python -m scripts.gate --contribution <path> --results <path> [--timeout <seconds>]

Exit status 0 means only that the sandbox run passed; it never means human or orchestrator sign-off
occurred. The recorded `mergeable` field is always False in this version because sign-off is not
implemented in code, and merges to main still require the orchestrator. Contribution stdout/stderr
are never inspected and never persisted; the JSONL record holds the contribution's SHA-256, the
sandbox return code, whether it passed, a short reason, and the mergeable flag - nothing else.

Reasons: sandbox_passed, sandbox_failed (nonzero exit), sandbox_timeout (124), sandbox_invalid
(the runner rejected the inputs; recorded as 125), sandbox_error (any other failure inside the
runner; recorded as 126). A contribution whose bytes cannot be read fails before any record is
written - no hash is ever fabricated.

The four pre-existing lint/format/type/test commands in scripts/gate.ps1 are untouched by this
module; it adds the sandbox result alongside them and never converts their failures into success.
"""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from openagentsearch.sandbox.runner import TIMEOUT_RETURNCODE, run_python_in_sandbox

INVALID_RETURNCODE = 125
ERROR_RETURNCODE = 126


@dataclass(frozen=True)
class GateResult:
    contribution_sha256: str
    sandbox_returncode: int
    sandbox_passed: bool
    reason: str
    mergeable: bool


def _record(results_path: str | Path, result: GateResult) -> None:
    target = Path(results_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(asdict(result), separators=(",", ":")) + "\n")


def run_contribution_gate(
    contribution: str | Path, *, timeout: float = 30.0, results_path: str | Path
) -> GateResult:
    """Hash the contribution, run it once in the sandbox, append one JSONL record, return it."""
    digest = hashlib.sha256(Path(contribution).read_bytes()).hexdigest()  # raises before any record
    try:
        sandbox = run_python_in_sandbox(contribution, timeout=timeout)
    except ValueError:
        result = GateResult(digest, INVALID_RETURNCODE, False, "sandbox_invalid", False)
    except Exception:  # fail closed; never leak exception text into the record
        result = GateResult(digest, ERROR_RETURNCODE, False, "sandbox_error", False)
    else:
        passed = sandbox.passed and sandbox.returncode == 0
        if sandbox.returncode == TIMEOUT_RETURNCODE:
            reason = "sandbox_timeout"
        elif passed:
            reason = "sandbox_passed"
        else:
            reason = "sandbox_failed"
        result = GateResult(digest, sandbox.returncode, passed, reason, False)
    _record(results_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.gate")
    parser.add_argument("--contribution", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        result = run_contribution_gate(args.contribution, timeout=args.timeout, results_path=args.results)
    except OSError as exc:
        print(f"gate: cannot read contribution: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), separators=(",", ":")))
    return 0 if result.sandbox_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
