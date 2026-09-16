"""Regenerates the committed message-log fixture package B1 tests replay against:

  tests/fixtures/reputation/ledger-20.jsonl -- one room's `messages/<room>.jsonl` file (the same
  on-disk row shape `openagentsearch.sources.technocore_messages.MessageLog` writes), covering 20
  synthetic DIDs plus the deliberately-malformed/unsigned rows `tests/test_reputation_facts.py`
  and `tests/test_reputation_ledger.py` check for.

Deterministic: every DID, timestamp and piece of text is a pure function of this script's own
constants (`NOW`, the per-letter age/post-count table below) -- nothing here reads a clock or
random source. `main()` self-checks that determinism (two independent builds, byte-compared)
before writing anything, the same convention `scripts/make_lexical_fixture.py` uses.

`NOW` (epoch seconds) is a fixed constant that test files hardcode too (see the comment on `NOW`
below and every test that computes scores against this fixture) -- there is deliberately no
import of this script from `tests/` (`scripts/` is not a package; see
`scripts/make_lexical_fixture.py`'s own note), so keep the two literals in sync by hand if either
ever changes.

Usage: python scripts/make_reputation_fixture.py
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "reputation" / "ledger-20.jsonl"

ROOM = "b1"
DAY_S = 86400.0

# Fixed epoch seconds this fixture is built "as of". Every DID's age below is computed relative
# to this constant. Tests that score this fixture must pass this SAME value as `now=`.
NOW = 1_758_000_000.0

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _short_id(seed: str, length: int) -> str:
    """`length` base58 characters deterministically derived from `seed` (SHA-256 of `seed:N` for
    increasing `N`, base58-encoded, concatenated until long enough, then truncated). Not a real
    key derivation -- only for short, deterministic, collision-controllable synthetic
    identifiers: two calls with the same `seed` always return the same string, which is exactly
    how the E/F ambiguous-suffix pair below is constructed."""
    out = ""
    counter = 0
    while len(out) < length:
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        n = int.from_bytes(digest, "big")
        chunk: list[str] = []
        while n > 0 and len(chunk) < length:
            n, rem = divmod(n, 58)
            chunk.append(_BASE58[rem])
        out += "".join(chunk)
        counter += 1
    return out[:length]


def _did(label: str, *, suffix_seed: str | None = None) -> str:
    """A short, deterministic, syntactically did:key:z-shaped identifier: `did:key:z` + a 4-char
    body (unique per `label`) + an 8-char suffix (unique per `suffix_seed`, defaulting to
    `label`) -- the suffix IS the DID's last 8 characters, exactly what
    `openagentsearch.reputation.facts.mention_targets` resolves `@`-mentions against."""
    body = _short_id(f"body:{label}", 4)
    suffix = _short_id(suffix_seed if suffix_seed is not None else f"suffix:{label}", 8)
    return f"did:key:z{body}{suffix}"


def _ts(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ---- the 20 DIDs -----------------------------------------------------------------------------

DID_A = _did("A")
DID_B = _did("B")
DID_C = _did("C")
DID_D = _did("D")
COLLIDE_SUFFIX = _short_id("collide", 8)
DID_E = _did("E", suffix_seed="collide")
DID_F = _did("F", suffix_seed="collide")
DID_G = _did("G")

REMAINING_LETTERS = "HIJKLMNOPQRST"  # 13 more DIDs -> 20 total with A..G above
REMAINING_DIDS = {letter: _did(letter) for letter in REMAINING_LETTERS}
DID_H = REMAINING_DIDS["H"]

assert len({DID_A, DID_B, DID_C, DID_D, DID_E, DID_F, DID_G, *REMAINING_DIDS.values()}) == 20


class _Row:
    __slots__ = ("ts", "sender", "text", "sig", "nonce", "bad_ts")

    def __init__(
        self,
        ts: float,
        sender: str,
        text: str,
        *,
        sig: str = "s",
        nonce: str = "",
        bad_ts: bool = False,
    ):
        self.ts = ts
        self.sender = sender
        self.text = text
        self.sig = sig
        self.nonce = nonce
        self.bad_ts = bad_ts


def _build_rows() -> list[_Row]:
    rows: list[_Row] = []

    # DID A: 3 posts, first seen exactly 60 days before NOW, all distinct text.
    a_first = NOW - 60 * DAY_S
    rows.append(_Row(a_first, DID_A, "hello from A one"))
    rows.append(_Row(a_first + 3600.0, DID_A, "hello from A two"))
    rows.append(_Row(a_first + 7200.0, DID_A, "hello from A three"))

    # DID B: 200 posts of the identical text, 2s apart (200 within a single 60s window at any
    # point in the run => a per-DID rate burst; the whole run spans <7 minutes, well inside "one
    # hour"). First seen 10 days before NOW -- old enough that, ABSENT the burst rule, this would
    # otherwise look like a normal, aged identity; the burst rule must override that.
    b_first = NOW - 10 * DAY_S
    for i in range(200):
        rows.append(_Row(b_first + i * 2.0, DID_B, "spam"))

    # DID C: mentions DID A by its full did:key token; two of its three posts repeat text.
    c_first = NOW - 45 * DAY_S
    rows.append(_Row(c_first, DID_C, "gm all"))
    rows.append(_Row(c_first + 60.0, DID_C, "gm all"))
    rows.append(_Row(c_first + 120.0, DID_C, f"welcome {DID_A} glad youre here"))

    # DID D: mentions DID A by its short @-suffix form; both posts distinct.
    d_first = NOW - 20 * DAY_S
    rows.append(_Row(d_first, DID_D, f"hi @{DID_A[-8:]} good to see you"))
    rows.append(_Row(d_first + 60.0, DID_D, "another day another post"))

    # DID E and DID F: two DISTINCT DIDs sharing the same last-8-character suffix (COLLIDE_SUFFIX)
    # -- an @-mention using that suffix must resolve to neither of them.
    e_first = NOW - 5 * DAY_S
    rows.append(_Row(e_first, DID_E, "morning"))
    rows.append(_Row(e_first + 60.0, DID_E, "afternoon"))
    f_first = NOW - 70 * DAY_S
    rows.append(_Row(f_first, DID_F, "old timer checking in"))

    # DID G: one post uses the ambiguous @-suffix -> counted in G's own unresolved_mentions, not
    # resolved to DID_E or DID_F.
    g_first = NOW - 15 * DAY_S
    rows.append(_Row(g_first, DID_G, "just chatting"))
    rows.append(_Row(g_first + 60.0, DID_G, f"hey @{COLLIDE_SUFFIX} check this out"))

    # DID H: one extra row with an unparseable ts (never becomes a Post; H's own real posts are
    # below, in the REMAINING_LETTERS loop, and are unaffected by this row). Its own placeholder
    # `ts` (used only to place it in the file's seq order) is otherwise unused -- see
    # `_rows_to_jsonl`, which substitutes a literal unparseable string for `bad_ts` rows.
    rows.append(_Row(NOW - 40 * DAY_S, DID_H, "this row's ts is corrupted", bad_ts=True))

    # H..T (13 DIDs, including H's real posts): varied ages (3 to 87 days) and varied
    # distinct-text ratios (even index -> every post distinct; odd index -> every post identical).
    for idx, letter in enumerate(REMAINING_LETTERS):
        did = REMAINING_DIDS[letter]
        age_days = 3 + idx * 7
        post_count = 1 + (idx % 3)
        first_ts = NOW - age_days * DAY_S
        for k in range(post_count):
            text = f"note {letter} {k}" if idx % 2 == 0 else "same text repeated"
            rows.append(_Row(first_ts + k * 60.0, did, text))

    # Two unsigned rows from a bare, non-DID sender name -- excluded as posts, counted as
    # skipped_unsigned.
    rows.append(_Row(NOW - 1 * DAY_S, "k3w", "hi everyone", sig=""))
    rows.append(_Row(NOW - 1 * DAY_S + 30.0, "k3w", "hi again", sig=""))

    rows.sort(key=lambda r: r.ts)
    return rows


def _rows_to_jsonl(rows: list[_Row]) -> bytes:
    lines: list[str] = []
    for seq, row in enumerate(rows):
        ts_str = "not-a-real-timestamp" if row.bad_ts else _ts(row.ts)
        obj = {
            "room": ROOM,
            "seq": seq,
            "ts": ts_str,
            "sender": row.sender,
            "text": row.text,
            "sig": row.sig,
            "nonce": row.nonce,
            "observed_at": row.ts,
        }
        lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)

    bytes1 = _rows_to_jsonl(_build_rows())
    bytes2 = _rows_to_jsonl(_build_rows())
    if bytes1 != bytes2:
        raise AssertionError("ledger-20.jsonl is not deterministic across two builds")

    FIXTURE_PATH.write_bytes(bytes1)
    print(
        json.dumps(
            {
                "fixture_path": str(FIXTURE_PATH),
                "fixture_bytes": len(bytes1),
                "rows": bytes1.count(b"\n"),
                "did_a": DID_A,
                "did_b": DID_B,
                "room": ROOM,
                "now": NOW,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
