"""Audit-hook guard installed inside a sandboxed Python child (P6.1).

What this is: process-level isolation for Python contribution checks. Once installed, the hook denies
network access, process spawning, and file opens outside the sandbox root by raising SandboxViolation
from the audited call. Reads (never writes) of the interpreter's own installation - sys.prefix and
friends - stay allowed so imports and tracebacks keep working; nothing else outside the root is
reachable through open().

What this is NOT: an OS security boundary, a VM, a container, or protection against hostile native
code. A C extension, ctypes, or a bug in CPython can bypass an audit hook. Environment isolation is
not attempted here at all: CPython emits no reliable audit event for ordinary os.environ reads, so
secrets are kept out of the child by the parent runner never inheriting them (see runner.py).

The module is loaded directly from a copied file by the runner's bootstrap; it must stay stdlib-only
and free of openagentsearch imports.
"""

import os
import sys
from pathlib import Path

DENIED_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.startfile"}
)
DENIED_PREFIXES = ("socket.", "os.exec")
WRITE_FLAGS = getattr(os, "O_WRONLY", 0) | getattr(os, "O_RDWR", 0) | getattr(os, "O_APPEND", 0) | getattr(os, "O_CREAT", 0) | getattr(os, "O_TRUNC", 0)


class SandboxViolation(PermissionError):
    """Raised from inside an audited call when the contribution attempts a denied operation."""


def _resolve(path: object) -> str | None:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        return os.path.normcase(str(Path(os.fspath(path)).resolve()))
    except (OSError, ValueError, RuntimeError):
        return None


def _under(target: str, root: str) -> bool:
    return target == root or target.startswith(root + os.sep)


def _is_write(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & WRITE_FLAGS)


def install_guard(allowed_root: str | Path) -> None:
    """Install the audit hook. Reads and writes are confined to allowed_root; the interpreter's own
    installation directories are additionally readable; sockets and process creation are denied."""
    root = os.path.normcase(str(Path(allowed_root).resolve()))
    read_only_roots = set()
    for candidate in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, os.path.dirname(sys.executable)):
        resolved = _resolve(candidate)
        if resolved:
            read_only_roots.add(resolved)

    def hook(event: str, args: tuple) -> None:
        if event.startswith(DENIED_PREFIXES) or event in DENIED_EVENTS:
            raise SandboxViolation(f"SandboxViolation: {event} is denied inside the sandbox")
        if event != "open":
            return
        path = args[0] if args else None
        if path is None or isinstance(path, int):  # None / file descriptors: nothing new is reachable
            return
        target = _resolve(path)
        if target is None:
            raise SandboxViolation(f"SandboxViolation: unresolvable path {path!r} denied")
        if _under(target, root):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        if not _is_write(mode, flags) and any(_under(target, ro) for ro in read_only_roots):
            return
        raise SandboxViolation(f"SandboxViolation: open of {os.fspath(path)!r} outside the sandbox root is denied")

    sys.addaudithook(hook)
