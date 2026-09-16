"""Shared constants and helpers for `tests/test_reputation_*.py`.

NOT itself a test module -- no `test_` prefix, so pytest never collects it. Loads
`scripts/make_reputation_fixture.py` (which is not an importable package -- see that script's own
module docstring) by file path, once, so every test file that needs `NOW`, `ROOM`, or one of the
fixture's DIDs reads them from the SAME source of truth the committed fixture was generated from,
rather than a second, hand-copied literal that could silently drift out of sync.
"""

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

from openagentsearch.reputation.facts import Post

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "reputation" / "ledger-20.jsonl"
GENERATOR_PATH = REPO_ROOT / "scripts" / "make_reputation_fixture.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_reputation_fixture", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GEN = _load_generator()

NOW: float = _GEN.NOW
ROOM: str = _GEN.ROOM
DID_A: str = _GEN.DID_A
DID_B: str = _GEN.DID_B
DID_C: str = _GEN.DID_C
DID_D: str = _GEN.DID_D
DID_E: str = _GEN.DID_E
DID_F: str = _GEN.DID_F
DID_G: str = _GEN.DID_G
DID_H: str = _GEN.DID_H
COLLIDE_SUFFIX: str = _GEN.COLLIDE_SUFFIX
REMAINING_DIDS: dict[str, str] = _GEN.REMAINING_DIDS
ALL_20_DIDS: tuple[str, ...] = tuple(
    sorted({DID_A, DID_B, DID_C, DID_D, DID_E, DID_F, DID_G, *REMAINING_DIDS.values()})
)


def install_fixture(log_root: Path) -> Path:
    """Copy the committed `ledger-20.jsonl` into `log_root/messages/<ROOM>.jsonl` and return
    `log_root` (a usable `load_posts`/`build_ledger` root)."""
    messages_dir = log_root / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_PATH, messages_dir / f"{ROOM}.jsonl")
    return log_root


def make_burst_posts(
    *,
    count: int = 2000,
    start_ts: float,
    window_s: float = 40.0,
    room: str = ROOM,
    mention_did: str = DID_A,
    seq_start: int = 1_000_000,
) -> list[Post]:
    """`count` synthetic `Post`s, one distinct identity each, first-seen timestamps spread evenly
    across `window_s` seconds starting at `start_ts` (so every one of them falls inside a single
    `burst_window_s`-sized sweep window whenever `window_s <= burst_window_s`) -- one post per
    identity, every post's text distinct, every post mentioning `mention_did` by its full
    `did:key:z...` token. Deterministic: the same arguments always produce the same posts."""
    posts: list[Post] = []
    span = window_s if count <= 1 else window_s / (count - 1)
    for i in range(count):
        ts = start_ts + i * span
        sender = f"did:key:zburstidentity{i:05d}"
        text = f"hello I am burst identity number {i}, greetings {mention_did}"
        posts.append(
            Post(room=room, seq=seq_start + i, ts=ts, sender=sender, text=text, signed=True)
        )
    return posts
