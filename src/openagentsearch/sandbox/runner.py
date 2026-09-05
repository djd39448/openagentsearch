"""Run an untrusted Python contribution in a restricted subprocess (P6.1).

Isolation provided: a fresh interpreter (-I, so no user site, no PYTHON* variables, no script-dir on
sys.path), an isolated temporary working directory that is also the child's TEMP/HOME, an environment
built from scratch that never inherits secret-looking variables, and the audit-hook guard from guard.py
(no sockets, no process spawning, no file opens outside the sandbox root except read-only access to the
interpreter's own installation). Limits: this is process-level isolation for Python code only, not an OS
security boundary, VM, or container, and it does not defend against hostile native code.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

TIMEOUT_RETURNCODE = 124
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE")
# The only parent variables ever copied: what the interpreter needs to start on this platform.
BASE_ENV_KEYS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if os.name == "nt" else ("PATH", "LANG", "LC_ALL", "LC_CTYPE")

_BOOTSTRAP = """\
import importlib.util
import runpy
import sys

_spec = importlib.util.spec_from_file_location("_oas_sandbox_guard", {guard!r})
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
_guard.install_guard({root!r})
sys.argv = [{contribution!r}]
runpy.run_path({contribution!r}, run_name="__main__")
"""


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    passed: bool


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def _child_env(root: Path, allowed_env: Mapping[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None and not _looks_secret(key):
            env[key] = value
    for key in ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE"):
        env[key] = str(root)
    if allowed_env:
        for key, value in allowed_env.items():
            if not isinstance(key, str) or not isinstance(value, str) or not key:
                raise ValueError("allowed_env must map non-empty str names to str values")
            if _looks_secret(key):
                raise ValueError(f"allowed_env entry {key!r} looks like a secret and is refused")
            env[key] = value
    return env


def _text(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def run_python_in_sandbox(
    script: str | Path, *, timeout: float = 30.0, allowed_env: Mapping[str, str] | None = None
) -> SandboxResult:
    """Copy `script` into a fresh sandbox directory and run it there under the guard.

    passed is exactly `returncode == 0`; contribution output is never parsed. A timeout returns
    returncode 124 with whatever output was captured, never an exception.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError("timeout must be a positive number of seconds")
    source = Path(script)
    if source.suffix.lower() != ".py" or not source.is_file():
        raise ValueError(f"script must be an existing regular .py file: {source}")

    with tempfile.TemporaryDirectory(prefix="oas-sandbox-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve()
        env = _child_env(root, allowed_env)  # validated before anything is copied or launched
        contribution = root / "contribution.py"
        guard_copy = root / "_oas_guard.py"
        bootstrap = root / "_oas_bootstrap.py"
        shutil.copyfile(source, contribution)
        shutil.copyfile(Path(__file__).with_name("guard.py"), guard_copy)
        bootstrap.write_text(
            _BOOTSTRAP.format(guard=str(guard_copy), root=str(root), contribution=str(contribution)),
            encoding="utf-8",
        )
        command = [sys.executable, "-I", "-X", "utf8", str(bootstrap)]
        try:
            completed = subprocess.run(
                command, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(TIMEOUT_RETURNCODE, _text(exc.stdout), _text(exc.stderr), False)
        return SandboxResult(
            completed.returncode, _text(completed.stdout), _text(completed.stderr), completed.returncode == 0
        )
