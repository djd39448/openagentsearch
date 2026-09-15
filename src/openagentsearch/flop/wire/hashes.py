"""Hash and domain-preimage builders for the FLOP v1 wire objects. All pure (no I/O, no randomness,
no clock) and every non-trivial one is corpus-verified: the preimage byte layout AND the resulting
digest were checked against `tests/fixtures/flop/wire-format-v1.json` at implementation time (see
`docs/flop-wire.md`'s hash table).

Two hash algorithms are used, and which object uses which is NOT interchangeable or guessable from
the yellow paper text alone (see `docs/flop-wire.md`):

- `blake2_256` = `hashlib.blake2b(data, digest_size=32)` -- `channel_id`, `task_hash`, leaf hashes
  V0-V3, Merkle node/root.
- `sha256` = `hashlib.sha256` -- `decode_policy_hash`, `report_data` (the only hash the corpus
  names explicitly, as `decode_policy_v1.sha256_hex`).

Every preimage builder here is also exposed as its own function (not just the hashed result) so
callers -- and this package's own tests -- can assert against the corpus's `*_preimage_hex` /
`*_hex` fields independently: a preimage that is byte-correct but hashed with the wrong algorithm,
or vice versa, is two different, separately-catchable bugs.

NOT guaranteed: nothing here proves these are the *only* wire-format hashes FLOP v1 uses -- the
F.4 audit-challenge hash (`blake2(audit_id || shard)`, yellow paper line 1546) has zero vectors in
the corpus and is not implemented here at all (see the package docstring). Every function raises
plain `ValueError` (never `WireDecodeError`) on caller misuse (wrong-length hash, out-of-range
integer, wrong type) -- these are pure functions with no notion of "malformed wire bytes", only
"caller passed something that cannot be this field."
"""

import hashlib
from collections.abc import Sequence
from typing import Protocol

_U64_MAX = 2**64 - 1
_U128_MAX = 2**128 - 1

_TASK_HASH_DOMAIN = b"FLOP/POUI/TASK"
_CHANNEL_ID_DOMAIN = b"FLOP/COMPUTE_CHANNEL/ID"
_DECODE_POLICY_HASH_DOMAIN = b"FLOP_DECODE_POLICY_HASH_V1"
_RECEIPT_DOMAIN = b"FLOP/COMPUTE_CHANNEL/RECEIPT"
_WIRE_VERSION_1 = b"\x01"


def _require_h256(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes (H256), got {value!r}")


def _require_uint(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < 0 or value > maximum:
        raise ValueError(f"{name} must be in [0, {maximum}], got {value!r}")


def blake2_256(data: bytes) -> bytes:
    """`hashlib.blake2b(data, digest_size=32).digest()` -- Substrate's `blake2_256`, corpus-proven
    to be exactly this call (six independent preimage/hash pairs across three object families,
    plus the two-step Merkle-path recomputation). Not `blake2s`; the two are different algorithms
    with different outputs and `blake2s` is not exercised by any corpus vector."""
    if not isinstance(data, bytes):
        raise ValueError(f"data must be bytes, got {data!r}")
    return hashlib.blake2b(data, digest_size=32).digest()


def sha256(data: bytes) -> bytes:
    """`hashlib.sha256(data).digest()`."""
    if not isinstance(data, bytes):
        raise ValueError(f"data must be bytes, got {data!r}")
    return hashlib.sha256(data).digest()


# --- task_hash v1 (F.1; 183 B preimage; blake2_256) -------------------------------------------


def task_hash_v1_preimage(
    genesis_hash: bytes,
    agent: bytes,
    nonce: int,
    model_hash: bytes,
    payload_hash: bytes,
    commit_hash: bytes,
) -> bytes:
    """`"FLOP/POUI/TASK"`(14) || `0x01`(1) || `genesis_hash`(32) || `agent`(32) || `nonce:u64LE`(8)
    || `model_hash`(32) || `payload_hash`(32) || `commit_hash`(32) = 183 B exactly, corpus-verified
    against `direct_rail_v1.task_hash.preimage_hex`."""
    _require_h256("genesis_hash", genesis_hash)
    _require_h256("agent", agent)
    _require_uint("nonce", nonce, _U64_MAX)
    _require_h256("model_hash", model_hash)
    _require_h256("payload_hash", payload_hash)
    _require_h256("commit_hash", commit_hash)
    return (
        _TASK_HASH_DOMAIN
        + _WIRE_VERSION_1
        + genesis_hash
        + agent
        + nonce.to_bytes(8, "little")
        + model_hash
        + payload_hash
        + commit_hash
    )


def task_hash_v1(
    genesis_hash: bytes,
    agent: bytes,
    nonce: int,
    model_hash: bytes,
    payload_hash: bytes,
    commit_hash: bytes,
) -> bytes:
    """`blake2_256(task_hash_v1_preimage(...))`. Only the F.1 "v1" formula (with domain, version
    and `genesis_hash`) is implemented -- the yellow paper's symbol table also gives a shorter,
    undomained `task_hash = blake2_256(agent||nonce||model_hash||payload_hash||commit_hash)`
    formula with no corpus vector; that formula is NOT implemented here (see
    `docs/flop-wire.md`)."""
    return blake2_256(
        task_hash_v1_preimage(genesis_hash, agent, nonce, model_hash, payload_hash, commit_hash)
    )


# --- channel_id v1 (F.1; 128 B preimage; blake2_256) -------------------------------------------


def channel_id_v1_preimage(genesis_hash: bytes, agent: bytes, miner: bytes, nonce: int) -> bytes:
    """`"FLOP/COMPUTE_CHANNEL/ID"`(23) || `0x01`(1) || `genesis_hash`(32) || `agent`(32) ||
    `miner`(32) || `nonce:u64LE`(8) = 128 B exactly, corpus-verified against
    `compute_channel_v1.channel_id.preimage_hex`."""
    _require_h256("genesis_hash", genesis_hash)
    _require_h256("agent", agent)
    _require_h256("miner", miner)
    _require_uint("nonce", nonce, _U64_MAX)
    return (
        _CHANNEL_ID_DOMAIN
        + _WIRE_VERSION_1
        + genesis_hash
        + agent
        + miner
        + nonce.to_bytes(8, "little")
    )


def channel_id_v1(genesis_hash: bytes, agent: bytes, miner: bytes, nonce: int) -> bytes:
    """`blake2_256(channel_id_v1_preimage(...))`."""
    return blake2_256(channel_id_v1_preimage(genesis_hash, agent, miner, nonce))


# --- decode_policy_hash v1 (F.1; 152 B preimage; sha256) ----------------------------------------


def decode_policy_hash_v1_preimage(scale_bytes: bytes) -> bytes:
    """`"FLOP_DECODE_POLICY_HASH_V1"`(26) || `scale_bytes`. For the corpus's `DecodePolicy` (126
    B), this is 152 B exactly, corpus-verified against `decode_policy_v1.hash_preimage_hex`. Does
    NOT itself require `scale_bytes` to be exactly 126 B -- `DecodePolicy.encode()` can be longer
    when `class=Other(u16)` or `output_transform=TransformId(H256)` is set; this builder accepts
    any non-empty SCALE encoding."""
    if not isinstance(scale_bytes, bytes) or not scale_bytes:
        raise ValueError(f"scale_bytes must be non-empty bytes, got {scale_bytes!r}")
    return _DECODE_POLICY_HASH_DOMAIN + scale_bytes


def decode_policy_hash_v1(scale_bytes: bytes) -> bytes:
    """`sha256(decode_policy_hash_v1_preimage(scale_bytes))` -- the one F.1 hash that is SHA256,
    not blake2_256 (corpus-verified: it is the corpus's only explicitly-named hash algorithm, as
    `decode_policy_v1.sha256_hex`)."""
    return sha256(decode_policy_hash_v1_preimage(scale_bytes))


# --- report_data v1 (F.1; 145 B preimage -> 64 B zero-padded result; sha256) --------------------


def report_data_v1_preimage(
    task_hash: bytes,
    gn_weight: int,
    latency_ms: int,
    model_hash: bytes,
    output_hash: bytes,
    decode_policy_hash: bytes,
    tee_type_tag: int,
) -> bytes:
    """`task_hash`(32) || `gn_weight:u64LE`(8) || `latency_ms:u64LE`(8) || `model_hash`(32) ||
    `output_hash`(32) || `decode_policy_hash`(32) || `tee_type` tag(1) = 145 B exactly,
    corpus-verified against `direct_rail_v1.report_data_preimage_hex`. `tee_type_tag` is the raw
    SCALE enum tag byte (0-255 accepted here; `objects.TeeType` restricts to the four known tags at
    decode time -- this builder does not re-enforce that restriction)."""
    _require_h256("task_hash", task_hash)
    _require_uint("gn_weight", gn_weight, _U64_MAX)
    _require_uint("latency_ms", latency_ms, _U64_MAX)
    _require_h256("model_hash", model_hash)
    _require_h256("output_hash", output_hash)
    _require_h256("decode_policy_hash", decode_policy_hash)
    _require_uint("tee_type_tag", tee_type_tag, 255)
    return (
        task_hash
        + gn_weight.to_bytes(8, "little")
        + latency_ms.to_bytes(8, "little")
        + model_hash
        + output_hash
        + decode_policy_hash
        + bytes([tee_type_tag])
    )


def report_data_v1(
    task_hash: bytes,
    gn_weight: int,
    latency_ms: int,
    model_hash: bytes,
    output_hash: bytes,
    decode_policy_hash: bytes,
    tee_type_tag: int,
) -> bytes:
    """`sha256(report_data_v1_preimage(...))`(32) || `0x00 * 32` = 64 B exactly, matching the
    64-byte Intel TDX/SGX `REPORTDATA` field convention (corpus-verified: trailing 32 bytes of
    `direct_rail_v1.report_data_hex` are confirmed all-zero)."""
    digest = sha256(
        report_data_v1_preimage(
            task_hash, gn_weight, latency_ms, model_hash, output_hash, decode_policy_hash,
            tee_type_tag,
        )
    )
    return digest + b"\x00" * 32


# --- agent receipt v1 (F.1; 125 B message, signed directly -- not hashed first) -----------------


def receipt_message_v1(
    channel_id: bytes, final_root: bytes, aggregate_gn: int, payable: int
) -> bytes:
    """`"FLOP/COMPUTE_CHANNEL/RECEIPT"`(28) || `0x01`(1) || `channel_id`(32) || `final_root`(32) ||
    `aggregate_gn:u128LE`(16) || `payable:u128LE`(16) = 125 B exactly, corpus-verified against
    `compute_channel_v1.receipt.preimage_hex`. This is the raw message sr25519 signs directly (no
    hash-first step) -- unlike the leaf-hash signature, which signs a 32-byte hash."""
    _require_h256("channel_id", channel_id)
    _require_h256("final_root", final_root)
    _require_uint("aggregate_gn", aggregate_gn, _U128_MAX)
    _require_uint("payable", payable, _U128_MAX)
    return (
        _RECEIPT_DOMAIN
        + _WIRE_VERSION_1
        + channel_id
        + final_root
        + aggregate_gn.to_bytes(16, "little")
        + payable.to_bytes(16, "little")
    )


def legacy_receipt_message(
    channel_id: bytes, final_root: bytes, aggregate_gn: int, payable: int
) -> bytes:
    """`channel_id`(32) || `final_root`(32) || `aggregate_gn:u128LE`(16) || `payable:u128LE`(16) =
    96 B exactly -- the historical untagged receipt preimage: `receipt_message_v1`'s same four
    trailing fields with the 28-byte domain string and 1-byte version stripped (corpus-verified
    against the `legacy_receipt_current_channel` negative case: the 96-byte message component
    equals this exactly). Historical-profile-only; do not sign new receipts this way."""
    _require_h256("channel_id", channel_id)
    _require_h256("final_root", final_root)
    _require_uint("aggregate_gn", aggregate_gn, _U128_MAX)
    _require_uint("payable", payable, _U128_MAX)
    return (
        channel_id
        + final_root
        + aggregate_gn.to_bytes(16, "little")
        + payable.to_bytes(16, "little")
    )


# --- leaf-hash preimages V0-V3 (F.3; blake2_256; no domain prefix, leads with channel_id) --------


class LeafFields(Protocol):
    """Structural shape `leaf_preimage`/`leaf_hash` need from a turn -- satisfied by
    `objects.VerifiedTurn` without this module importing `objects` (which imports this module).
    Members are read-only properties so a frozen dataclass satisfies the protocol."""

    @property
    def turn_index(self) -> int: ...

    @property
    def h_in(self) -> bytes: ...

    @property
    def h_out(self) -> bytes: ...

    @property
    def g_n(self) -> int: ...

    @property
    def decode_policy_hash(self) -> bytes: ...

    @property
    def h_ids(self) -> bytes: ...

    @property
    def toploc_commitment_hash(self) -> bytes: ...

    @property
    def miner_recv_ms(self) -> int: ...

    @property
    def miner_done_ms(self) -> int: ...

    @property
    def latency_ms(self) -> int: ...


def leaf_preimage(version: int, channel_id: bytes, turn: LeafFields) -> bytes:
    """The version-specific leaf preimage (F.3, lines 1527-1530), all corpus-verified byte-for-byte
    and hash-for-hash against `compute_channel_v1.leaf_versions[*]`:

    - V0 (116 B): `channel_id`(32) || `turn_index:u32LE`(4) || `h_in`(32) || `h_out`(32) ||
      `g_n:u128LE`(16).
    - V1 (140 B): V0 || `miner_recv_ms:u64LE`(8) || `miner_done_ms:u64LE`(8) ||
      `latency_ms:u64LE`(8).
    - V2 (172 B): V0 || `decode_policy_hash`(32) || [V1's three timing fields, 24 B].
    - V3 (236 B): V0 || `decode_policy_hash`(32) || `h_ids`(32) || `toploc_commitment_hash`(32) ||
      [timing, 24 B].

    No domain prefix -- `channel_id` (itself already domain-separated, see `channel_id_v1`) is the
    leading field. `version` is the raw `LeafVersion` tag (0-3); any other value raises `ValueError`
    (caller misuse -- unknown-tag rejection during *decode* is `objects.decode_leaf_version`'s job,
    not this pure builder's).
    """
    if version not in (0, 1, 2, 3):
        raise ValueError(f"version must be 0-3, got {version!r}")
    _require_h256("channel_id", channel_id)
    _require_uint("turn_index", turn.turn_index, 2**32 - 1)
    _require_h256("h_in", turn.h_in)
    _require_h256("h_out", turn.h_out)
    _require_uint("g_n", turn.g_n, _U128_MAX)
    base = (
        channel_id
        + turn.turn_index.to_bytes(4, "little")
        + turn.h_in
        + turn.h_out
        + turn.g_n.to_bytes(16, "little")
    )
    if version == 0:
        return base
    _require_uint("miner_recv_ms", turn.miner_recv_ms, _U64_MAX)
    _require_uint("miner_done_ms", turn.miner_done_ms, _U64_MAX)
    _require_uint("latency_ms", turn.latency_ms, _U64_MAX)
    timing = (
        turn.miner_recv_ms.to_bytes(8, "little")
        + turn.miner_done_ms.to_bytes(8, "little")
        + turn.latency_ms.to_bytes(8, "little")
    )
    if version == 1:
        return base + timing
    _require_h256("decode_policy_hash", turn.decode_policy_hash)
    if version == 2:
        return base + turn.decode_policy_hash + timing
    _require_h256("h_ids", turn.h_ids)
    _require_h256("toploc_commitment_hash", turn.toploc_commitment_hash)
    return base + turn.decode_policy_hash + turn.h_ids + turn.toploc_commitment_hash + timing


def leaf_hash(version: int, channel_id: bytes, turn: LeafFields) -> bytes:
    """`blake2_256(leaf_preimage(version, channel_id, turn))`. The signature over this hash
    (`enclave_sig`, sr25519 per the yellow paper) is over these 32 bytes, not the raw preimage."""
    return blake2_256(leaf_preimage(version, channel_id, turn))


# --- Merkle node / path / root (F.3; blake2_256; no prefix) --------------------------------------


def merkle_node(left: bytes, right: bytes) -> bytes:
    """`blake2_256(left(32) || right(32))`, 64 B preimage, no prefix. `sha256` does NOT reproduce
    the corpus's `merkle.root_hex` under the same path walk -- independently ruling out sha256 for
    node hashing, not merely unproven."""
    _require_h256("left", left)
    _require_h256("right", right)
    return blake2_256(left + right)


def merkle_root_from_path(leaf_hash_value: bytes, path: Sequence[tuple[bytes, bool]]) -> bytes:
    """Walk `path` from `leaf_hash_value` up to a root. Each `(sibling, sibling_is_left)` step
    combines the current hash with `sibling`: when `sibling_is_left` is `True` the sibling is the
    LEFT input (`merkle_node(sibling, current)`); when `False` the sibling is the RIGHT input
    (`merkle_node(current, sibling)`) -- this `sibling_is_left` polarity is corpus-verified end to
    end (walking `compute_channel_v1.merkle.path_for_index_2` from the V3 leaf hash reproduces
    `merkle.root_hex` exactly under this rule).

    NOT guaranteed: no bound on `len(path)` is enforced here (64-entry `PathTooLong` is a
    decode-time / settlement-time check, in `objects.py` / `settle.py` -- this pure function will
    happily walk an arbitrarily long path if given one)."""
    _require_h256("leaf_hash_value", leaf_hash_value)
    current = leaf_hash_value
    for index, (sibling, sibling_is_left) in enumerate(path):
        _require_h256(f"path[{index}].sibling", sibling)
        if not isinstance(sibling_is_left, bool):
            raise ValueError(
                f"path[{index}].sibling_is_left must be a bool, got {sibling_is_left!r}"
            )
        if sibling_is_left:
            current = merkle_node(sibling, current)
        else:
            current = merkle_node(current, sibling)
    return current


def merkle_root_of_leaves(leaves: Sequence[bytes]) -> bytes:
    """Root of a Merkle tree built bottom-up from `leaves` in order: an odd last node at any level
    is duplicated before combining (corpus: `merkle.odd_node_behavior == "duplicate last"`); an
    empty `leaves` returns 32 zero bytes; a single leaf returns that leaf unchanged. Corpus-verified
    against `compute_channel_v1.merkle.root_hex` using `leaf_order`'s three leaf hashes."""
    if not leaves:
        return b"\x00" * 32
    for index, leaf in enumerate(leaves):
        _require_h256(f"leaves[{index}]", leaf)
    level = list(leaves)
    if len(level) == 1:
        return level[0]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [merkle_node(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]
