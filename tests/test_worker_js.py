"""Package C2b, spec Tests item 5: runs the dependency-free JavaScript suite
(`worker/test/*.test.mjs` -- tokenizer, ranking, router) as a subprocess and asserts it passes.

Skips with a reason when no `node` is on PATH (this Python suite must never require Node to be
installed). Deliberately does NOT run `worker/test-mcp/` -- that suite needs `worker/node_modules`
(`agents`, `@modelcontextprotocol/server`, `zod`), which this Python suite must not depend on; see
the repository's test recipes for the WSL-only command that runs it.

Every file is passed to `node --test` explicitly (as a sorted list, not a bare directory or glob
string) rather than `node --test worker/test`: on this box's Node (both the Windows 24.x and the
WSL 22.x install), a bare directory path with no trailing separator raises
`MODULE_NOT_FOUND` instead of being treated as a test directory to scan -- passing the resolved
file list sidesteps that entirely and is unaffected by shell/argv differences. Bounded by a
120-second subprocess timeout so a hang here can never hang the suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKER_TEST_DIR = REPO / "worker" / "test"
SUBPROCESS_TIMEOUT = 120


def test_worker_dependency_free_suite_passes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("no `node` executable on PATH; worker/test/ requires Node")

    test_files = sorted(str(p.relative_to(REPO)) for p in WORKER_TEST_DIR.glob("*.test.mjs"))
    assert test_files, f"no *.test.mjs files found under {WORKER_TEST_DIR}"

    proc = subprocess.run(
        [node, "--test", *test_files],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-60:])
        pytest.fail(
            f"node --test {' '.join(test_files)} exited {proc.returncode}\n--- tail ---\n{tail}",
            pytrace=False,
        )


def test_worker_test_mcp_directory_is_not_run_by_this_suite() -> None:
    """Documents the boundary this module relies on: `worker/test-mcp/` exists (it needs
    `node_modules`) but is never globbed or executed from here."""
    assert (REPO / "worker" / "test-mcp").is_dir()
    mcp_files = {p.name for p in WORKER_TEST_DIR.glob("*mcp*")}
    assert mcp_files == set(), f"worker/test/ must stay MCP-free, found: {mcp_files}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
