"""P6.1: untrusted contributions run in a restricted subprocess sandbox."""

import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from openagentsearch.sandbox.runner import SandboxResult, run_python_in_sandbox

TIMEOUT = 60.0


def _write(tmpdir: str, body: str, name: str = "contrib.py") -> Path:
    path = Path(tmpdir) / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_benign_contribution_runs_inside_its_own_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, """
            import json, hashlib, os, tempfile
            with open("note.txt", "w", encoding="utf-8") as fh:
                fh.write("hello from the sandbox")
            with open("note.txt", encoding="utf-8") as fh:
                text = fh.read()
            with tempfile.NamedTemporaryFile("w", delete=False) as scratch:
                scratch.write("scratch")
            print("MARKER-OK", json.dumps({"len": len(text)}), hashlib.sha256(text.encode()).hexdigest()[:8])
            print("cwd-has-note", os.path.exists("note.txt"))
            """)
        result = run_python_in_sandbox(script, timeout=TIMEOUT)
        assert isinstance(result, SandboxResult)
        assert result.returncode == 0, result.stderr
        assert result.passed is True
        assert "MARKER-OK" in result.stdout and '{"len": 22}' in result.stdout
        assert "cwd-has-note True" in result.stdout
        assert not (Path(tmpdir) / "note.txt").exists()  # ran in its own copy, not next to the source


def test_parent_secrets_are_not_inherited_but_allowed_env_is():
    name = "OAS_TEST_SECRET_TOKEN"
    value = "hunter2-do-not-leak-7f3a"
    os.environ[name] = value
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = _write(tmpdir, f"""
                import os
                print("secret=", repr(os.environ.get({name!r})))
                print("flag=", repr(os.environ.get("OAS_CONTRIB_FLAG")))
                print("names=", sorted(k for k in os.environ if "SECRET" in k.upper() or "TOKEN" in k.upper()))
                """)
            result = run_python_in_sandbox(script, timeout=TIMEOUT, allowed_env={"OAS_CONTRIB_FLAG": "visible"})
            assert result.passed is True, result.stderr
            assert "secret= None" in result.stdout
            assert value not in result.stdout and value not in result.stderr
            assert "flag= 'visible'" in result.stdout
            assert "names= []" in result.stdout
    finally:
        del os.environ[name]


def test_socket_creation_is_denied():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, """
            import socket
            print("before")
            s = socket.socket()
            print("SOCKET-CREATED")
            """)
        result = run_python_in_sandbox(script, timeout=TIMEOUT)
        assert result.returncode != 0
        assert result.passed is False
        assert "SandboxViolation" in result.stderr
        assert "before" in result.stdout and "SOCKET-CREATED" not in result.stdout


def test_reading_outside_the_sandbox_is_denied():
    with tempfile.TemporaryDirectory() as tmpdir:
        outside = Path(tmpdir) / "outside.txt"
        outside.write_text("TOP-SECRET-CONTENTS-91c4", encoding="utf-8")
        script = _write(tmpdir, f"""
            print("about to read")
            with open({str(outside.resolve())!r}, encoding="utf-8") as fh:
                print("LEAKED:", fh.read())
            """)
        result = run_python_in_sandbox(script, timeout=TIMEOUT)
        assert result.returncode != 0 and result.passed is False
        assert "SandboxViolation" in result.stderr
        assert "TOP-SECRET-CONTENTS-91c4" not in result.stdout
        assert "LEAKED" not in result.stdout
        assert outside.read_text(encoding="utf-8") == "TOP-SECRET-CONTENTS-91c4"


def test_spawning_a_subprocess_is_denied():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, """
            import subprocess, sys
            subprocess.run([sys.executable, "-c", "print('escaped')"])
            print("SPAWNED")
            """)
        result = run_python_in_sandbox(script, timeout=TIMEOUT)
        assert result.returncode != 0 and result.passed is False
        assert "SandboxViolation" in result.stderr
        assert "escaped" not in result.stdout and "SPAWNED" not in result.stdout


def test_invalid_inputs_and_timeouts():
    with tempfile.TemporaryDirectory() as tmpdir:
        script = _write(tmpdir, "print('x')\n")
        for bad_timeout in (0, -1, 0.0, None, "5", True):
            with pytest.raises(ValueError):
                run_python_in_sandbox(script, timeout=bad_timeout)
        with pytest.raises(ValueError):
            run_python_in_sandbox(_write(tmpdir, "print('x')\n", name="notes.txt"), timeout=TIMEOUT)
        with pytest.raises(ValueError):
            run_python_in_sandbox(Path(tmpdir) / "missing.py", timeout=TIMEOUT)
        with pytest.raises(ValueError):
            run_python_in_sandbox(Path(tmpdir), timeout=TIMEOUT)
        for secret_name in ("MY_API_KEY", "github_token", "DbPassword", "AWS_SECRET", "authorization", "COOKIE_JAR"):
            with pytest.raises(ValueError):
                run_python_in_sandbox(script, timeout=TIMEOUT, allowed_env={secret_name: "x"})
        looping = _write(tmpdir, """
            print("spinning", flush=True)
            while True:
                pass
            """, name="loop.py")
        result = run_python_in_sandbox(looping, timeout=1.0)
        assert result == SandboxResult(124, result.stdout, result.stderr, False)
        assert result.returncode == 124 and result.passed is False
        assert "spinning" in result.stdout
